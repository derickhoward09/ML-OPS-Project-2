#!/usr/bin/env python3
"""Hold an SSH connection before forwarding it to a local Cowrie listener.

The delay happens before the server identification string, so an SSH client
cannot start authentication (or run a command) while it waits. RFC 4253,
section 4.2 permits server lines before the identification string; they must
not start with ``SSH-``. Cowrie supplies the real identification string after
the delay. This process needs no privileges on port 22001.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass
import logging
import os
import signal


LOG = logging.getLogger("cowrie_delay_proxy")
PRE_BANNER_LINE = b"Please wait\r\n"
READ_SIZE = 16 * 1024


@dataclass(frozen=True)
class ProxyConfig:
    listen_host: str = "0.0.0.0"
    listen_port: int = 22001
    backend_host: str = "127.0.0.1"
    backend_port: int = 2222
    banner_delay: float = 885.0
    max_duration: float = 900.0
    keepalive_interval: float = 30.0
    backend_connect_timeout: float = 5.0
    max_connections: int = 128
    max_per_ip: int = 8
    max_prebanner_bytes: int = 64 * 1024

    def validate(self) -> None:
        if not 0 <= self.listen_port <= 65535:
            raise ValueError("listen_port must be in 0..65535")
        if not 1 <= self.backend_port <= 65535:
            raise ValueError("backend_port must be in 1..65535")
        if not 0 <= self.banner_delay < self.max_duration:
            raise ValueError("banner_delay must be nonnegative and below max_duration")
        if self.keepalive_interval <= 0 or self.backend_connect_timeout <= 0:
            raise ValueError("interval and timeout must be positive")
        if self.max_connections < 1 or self.max_per_ip < 1:
            raise ValueError("connection limits must be positive")
        if self.max_prebanner_bytes < 1:
            raise ValueError("max_prebanner_bytes must be positive")


class DelayProxy:
    def __init__(self, config: ProxyConfig):
        config.validate()
        self.config = config
        self.server: asyncio.AbstractServer | None = None
        self.active = 0
        self.active_per_ip: Counter[str] = Counter()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> asyncio.AbstractServer:
        self.server = await asyncio.start_server(
            self._accept,
            self.config.listen_host,
            self.config.listen_port,
            limit=self.config.max_prebanner_bytes,
        )
        for sock in self.server.sockets or ():
            LOG.info("listening on %s", sock.getsockname())
        return self.server

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        ip = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"
        loop = asyncio.get_running_loop()
        start = loop.time()
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)

        # asyncio runs the admission check and increments without an await, so
        # two concurrent connections cannot both claim the last available slot.
        admitted = self.active < self.config.max_connections and self.active_per_ip[ip] < self.config.max_per_ip
        if admitted:
            self.active += 1
            self.active_per_ip[ip] += 1
            LOG.info("arrival peer=%s active=%d", peer, self.active)
        else:
            LOG.warning("arrival rejected peer=%s active=%d per_ip=%d", peer, self.active, self.active_per_ip[ip])

        # A separate timer closes the public socket exactly at the deadline,
        # even if cancellation is still cleaning up a slow backend socket.
        deadline_timer = loop.call_at(start + self.config.max_duration, writer.close) if admitted else None
        reason = "closed"
        try:
            if admitted:
                try:
                    reason = await asyncio.wait_for(self._serve(reader, writer, start), self.config.max_duration)
                except asyncio.TimeoutError:
                    reason = "deadline"
                except (BrokenPipeError, ConnectionError, OSError) as exc:
                    reason = f"socket_error:{type(exc).__name__}"
                    LOG.info("connection error peer=%s error=%s", peer, exc)
            else:
                reason = "capacity"
        except asyncio.CancelledError:
            reason = "shutdown"
            raise
        finally:
            if deadline_timer is not None:
                deadline_timer.cancel()
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionError, OSError):
                pass
            if admitted:
                self.active -= 1
                self.active_per_ip[ip] -= 1
                if self.active_per_ip[ip] == 0:
                    del self.active_per_ip[ip]
            LOG.info("disconnect peer=%s reason=%s elapsed=%.1fs", peer, reason, loop.time() - start)
            if task is not None:
                self._tasks.discard(task)

    async def _serve(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        start: float,
    ) -> str:
        delay_result, pending_data = await self._delay_banner(client_reader, client_writer, start)
        if delay_result != "ready":
            return delay_result

        # Keep a little of the 15-second relay window for a backend failure or
        # slow connect, while the enclosing wait_for enforces the full deadline.
        remaining = self.config.max_duration - (asyncio.get_running_loop().time() - start)
        if remaining <= 0:
            return "deadline"
        try:
            backend_reader, backend_writer = await asyncio.wait_for(
                asyncio.open_connection(self.config.backend_host, self.config.backend_port),
                timeout=min(self.config.backend_connect_timeout, remaining),
            )
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            LOG.warning("Cowrie backend unavailable: %s", exc)
            return "backend_unavailable"

        try:
            if pending_data:
                backend_writer.write(pending_data)
                await backend_writer.drain()
            flows = {
                asyncio.create_task(self._relay(client_reader, backend_writer)),
                asyncio.create_task(self._relay(backend_reader, client_writer)),
            }
            try:
                # Closing either half of an SSH connection ends the session.
                done, pending = await asyncio.wait(flows, return_when=asyncio.FIRST_COMPLETED)
                for finished in done:
                    finished.result()
                return "relay_closed"
            finally:
                for flow in flows:
                    flow.cancel()
                await asyncio.gather(*flows, return_exceptions=True)
        finally:
            backend_writer.close()
            try:
                await asyncio.wait_for(backend_writer.wait_closed(), timeout=1.0)
            except (asyncio.TimeoutError, BrokenPipeError, ConnectionError, OSError):
                pass

    async def _delay_banner(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        start: float,
    ) -> tuple[str, bytes]:
        pending = bytearray()

        async def collect_early_data() -> str:
            while True:
                chunk = await reader.read(READ_SIZE)
                if not chunk:
                    return "client_closed"
                if len(pending) + len(chunk) > self.config.max_prebanner_bytes:
                    return "prebanner_limit"
                pending.extend(chunk)

        collector = asyncio.create_task(collect_early_data())
        loop = asyncio.get_running_loop()
        delay_end = start + self.config.banner_delay
        try:
            while True:
                remaining = delay_end - loop.time()
                if remaining <= 0:
                    break
                done, _ = await asyncio.wait({collector}, timeout=min(remaining, self.config.keepalive_interval))
                if done:
                    return collector.result(), b""
                if loop.time() < delay_end:
                    writer.write(PRE_BANNER_LINE)
                    await writer.drain()
            if collector.done():
                return collector.result(), b""
            return "ready", bytes(pending)
        finally:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)

    @staticmethod
    async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while chunk := await reader.read(READ_SIZE):
            writer.write(chunk)
            await writer.drain()


def parse_args() -> ProxyConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    fields = (
        ("listen-host", str, "0.0.0.0"),
        ("listen-port", int, 22001),
        ("backend-host", str, "127.0.0.1"),
        ("backend-port", int, 2222),
        ("banner-delay", float, 885.0),
        ("max-duration", float, 900.0),
        ("keepalive-interval", float, 30.0),
        ("backend-connect-timeout", float, 5.0),
        ("max-connections", int, 128),
        ("max-per-ip", int, 8),
        ("max-prebanner-bytes", int, 64 * 1024),
    )
    for flag, value_type, default in fields:
        env_name = "COWRIE_DELAY_" + flag.upper().replace("-", "_")
        parser.add_argument("--" + flag, type=value_type, default=os.environ.get(env_name, default))
    args = parser.parse_args()
    config = ProxyConfig(**vars(args))
    try:
        config.validate()
    except ValueError as exc:
        parser.error(str(exc))
    return config


async def run(config: ProxyConfig) -> None:
    proxy = DelayProxy(config)
    server = await proxy.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        await stop.wait()
    finally:
        server.close()
        await proxy.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(run(parse_args()))
