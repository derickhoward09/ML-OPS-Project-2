#!/usr/bin/env python3
"""Compare the local clock with an NTP server without setting the clock."""

import socket
import struct
import sys
import time
from datetime import datetime, timezone


NTP_EPOCH_OFFSET = 2_208_988_800
NTP_ERA_SECONDS = 2**32
NTP_PACKET_SIZE = 48
DEFAULT_TIMEOUT_SECONDS = 3.0


def ntp_timestamp(unix_time):
    """Encode a Unix timestamp in the 64-bit NTP timestamp format."""
    ntp_time = unix_time + NTP_EPOCH_OFFSET
    seconds = int(ntp_time) % NTP_ERA_SECONDS
    fraction = int((ntp_time - int(ntp_time)) * NTP_ERA_SECONDS)
    return struct.pack("!II", seconds, fraction)


def unix_timestamp(raw_timestamp, reference_unix_time):
    """Decode an NTP timestamp, choosing the era nearest the local date."""
    seconds, fraction = struct.unpack("!II", raw_timestamp)
    era = round((reference_unix_time + NTP_EPOCH_OFFSET - seconds) / NTP_ERA_SECONDS)
    return seconds + era * NTP_ERA_SECONDS - NTP_EPOCH_OFFSET + fraction / NTP_ERA_SECONDS


def iso_utc(unix_time):
    return datetime.fromtimestamp(unix_time, timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def query_ntp(server, timeout_seconds):
    """Return server-minus-local offset, local/server times, and round-trip time."""
    addresses = socket.getaddrinfo(server, 123, type=socket.SOCK_DGRAM)
    last_error = None

    for family, socktype, protocol, _, address in addresses:
        try:
            with socket.socket(family, socktype, protocol) as client:
                client.settimeout(timeout_seconds)
                client.connect(address)

                request = bytearray(NTP_PACKET_SIZE)
                request[0] = (4 << 3) | 3  # NTP version 4, client mode.
                local_send_time = time.time()
                request_transmit_timestamp = ntp_timestamp(local_send_time)
                request[40:48] = request_transmit_timestamp

                monotonic_send_time = time.monotonic()
                client.send(request)
                response = client.recv(512)
                monotonic_receive_time = time.monotonic()
                local_receive_time = time.time()

            if len(response) < NTP_PACKET_SIZE:
                raise ValueError("NTP response was shorter than 48 bytes")

            leap_indicator = response[0] >> 6
            version = (response[0] >> 3) & 0b111
            mode = response[0] & 0b111
            stratum = response[1]

            if leap_indicator == 3:
                raise ValueError("NTP server reports that its clock is unsynchronized")
            if version not in (3, 4) or mode != 4:
                raise ValueError("NTP response has an unexpected version or mode")
            if stratum == 0 or stratum > 15:
                raise ValueError("NTP server returned an invalid stratum")
            if response[24:32] != request_transmit_timestamp:
                raise ValueError("NTP response did not match this request")

            server_receive_time = unix_timestamp(response[32:40], local_receive_time)
            server_transmit_time = unix_timestamp(response[40:48], local_receive_time)
            if server_receive_time <= 0 or server_transmit_time < server_receive_time:
                raise ValueError("NTP response contains invalid server timestamps")

            offset = (
                (server_receive_time - local_send_time)
                + (server_transmit_time - local_receive_time)
            ) / 2
            round_trip = monotonic_receive_time - monotonic_send_time
            estimated_server_time = local_receive_time + offset
            return offset, local_receive_time, estimated_server_time, round_trip
        except OSError as error:
            last_error = error

    if last_error is not None:
        raise last_error
    raise OSError(f"no usable NTP addresses found for {server}")


def main():
    server = sys.argv[1] if len(sys.argv) > 1 else "time.nist.gov"
    tolerance_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0

    try:
        offset, local_time, nist_time, round_trip = query_ntp(server, DEFAULT_TIMEOUT_SECONDS)
    except Exception as error:  # Report network/protocol errors to the shell caller.
        print(f"NTP query failed: {error}", file=sys.stderr)
        return 1

    status = "warning" if abs(offset) > tolerance_seconds else "ok"
    direction = "behind" if offset > 0 else "ahead"
    print(
        "\t".join(
            (
                status,
                f"{offset:+.3f}",
                f"{abs(offset):.3f}",
                direction,
                iso_utc(local_time),
                iso_utc(nist_time),
                f"{round_trip:.3f}",
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
