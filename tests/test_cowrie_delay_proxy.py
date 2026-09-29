"""Socket-level checks for the SSH delay proxy, with subsecond timers."""

import asyncio
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cowrie"))
from delay_proxy import DelayProxy, PRE_BANNER_LINE, ProxyConfig


class DelayProxyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proxies = []
        self.backends = []

    async def asyncTearDown(self):
        for proxy in self.proxies:
            await proxy.close()
        for server in self.backends:
            server.close()
            await server.wait_closed()

    async def start_backend(self, handler):
        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.backends.append(server)
        return server.sockets[0].getsockname()[1]

    async def start_proxy(self, **kwargs):
        config = ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            banner_delay=0.10,
            max_duration=0.40,
            keepalive_interval=0.02,
            **kwargs,
        )
        proxy = DelayProxy(config)
        server = await proxy.start()
        self.proxies.append(proxy)
        return proxy, server.sockets[0].getsockname()[1]

    async def test_waits_then_forwards_buffered_client_banner_and_backend_banner(self):
        seen_client_banner = asyncio.Future()
        backend_connected = asyncio.Event()

        async def backend(reader, writer):
            backend_connected.set()
            writer.write(b"SSH-2.0-FakeCowrie\r\n")
            await writer.drain()
            seen_client_banner.set_result(await reader.readline())
            writer.close()
            await writer.wait_closed()

        backend_port = await self.start_backend(backend)
        _, proxy_port = await self.start_proxy(backend_port=backend_port)
        start = time.monotonic()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"SSH-2.0-TestClient\r\n")
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readline(), 0.2), PRE_BANNER_LINE)
        self.assertFalse(backend_connected.is_set())

        lines = []
        while True:
            line = await asyncio.wait_for(reader.readline(), 0.3)
            lines.append(line)
            if line.startswith(b"SSH-"):
                break
            self.assertEqual(line, PRE_BANNER_LINE)
        self.assertEqual(lines[-1], b"SSH-2.0-FakeCowrie\r\n")
        self.assertGreaterEqual(time.monotonic() - start, 0.09)
        self.assertEqual(await asyncio.wait_for(seen_client_banner, 0.2), b"SSH-2.0-TestClient\r\n")
        writer.close()
        await writer.wait_closed()

    async def test_early_disconnect_and_oversized_input_never_reach_cowrie(self):
        backend_connected = asyncio.Event()

        async def backend(_reader, writer):
            backend_connected.set()
            writer.close()
            await writer.wait_closed()

        backend_port = await self.start_backend(backend)
        proxy, proxy_port = await self.start_proxy(backend_port=backend_port, max_prebanner_bytes=32)

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.close()
        await writer.wait_closed()
        self.assertEqual(await asyncio.wait_for(reader.read(), 0.2), b"")

        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(b"x" * 33)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 0.2), b"")
        writer.close()
        await writer.wait_closed()
        self.assertFalse(backend_connected.is_set())
        self.assertEqual(proxy.active, 0)

    async def test_connection_caps_reject_excess_clients(self):
        async def backend(_reader, writer):
            writer.close()
            await writer.wait_closed()

        backend_port = await self.start_backend(backend)
        for max_connections, max_per_ip in ((1, 2), (2, 1)):
            with self.subTest(max_connections=max_connections, max_per_ip=max_per_ip):
                proxy, proxy_port = await self.start_proxy(
                    backend_port=backend_port,
                    max_connections=max_connections,
                    max_per_ip=max_per_ip,
                )
                first_reader, first_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
                second_reader, second_writer = await asyncio.open_connection("127.0.0.1", proxy_port)
                self.assertEqual(await asyncio.wait_for(second_reader.read(), 0.2), b"")
                self.assertEqual(proxy.active, 1)
                self.assertEqual(await asyncio.wait_for(first_reader.readline(), 0.2), PRE_BANNER_LINE)
                first_writer.close()
                second_writer.close()
                await first_writer.wait_closed()
                await second_writer.wait_closed()
                await asyncio.sleep(0.02)
                self.assertEqual(proxy.active, 0)

    async def test_connection_closes_at_total_deadline_during_relay(self):
        backend_connected = asyncio.Event()

        async def backend(reader, writer):
            backend_connected.set()
            writer.write(b"SSH-2.0-FakeCowrie\r\n")
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()

        backend_port = await self.start_backend(backend)
        config = ProxyConfig(
            listen_host="127.0.0.1",
            listen_port=0,
            backend_port=backend_port,
            banner_delay=0.03,
            max_duration=0.15,
            keepalive_interval=0.01,
        )
        proxy = DelayProxy(config)
        server = await proxy.start()
        self.proxies.append(proxy)
        proxy_port = server.sockets[0].getsockname()[1]
        start = time.monotonic()
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        while (line := await asyncio.wait_for(reader.readline(), 0.2)) != b"SSH-2.0-FakeCowrie\r\n":
            self.assertEqual(line, PRE_BANNER_LINE)
        self.assertTrue(backend_connected.is_set())
        self.assertEqual(await asyncio.wait_for(reader.read(), 0.3), b"")
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.13)
        self.assertLess(elapsed, 0.30)
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
