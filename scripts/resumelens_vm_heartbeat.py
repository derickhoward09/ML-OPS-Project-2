#!/usr/bin/env python3
"""Send a VM liveness ping to its dedicated Healthchecks.io check."""
import os
import re
import sys
import urllib.error
import urllib.request


def main() -> int:
    url = os.environ.get("VM_HEALTHCHECKS_PING_URL", "")
    if not re.fullmatch(r"https://hc-ping\.com/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", url):
        print("VM Healthchecks URL is missing or invalid.", file=sys.stderr)
        return 1
    request = urllib.request.Request(url, headers={"User-Agent": "ResumeLens-VM-Heartbeat/1"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if 200 <= response.status < 300:
                return 0
            print(f"VM Healthchecks returned HTTP {response.status}.", file=sys.stderr)
    except (OSError, urllib.error.URLError) as error:
        # Do not log URLs: the check UUID is a private ping credential.
        print(f"VM Healthchecks ping failed ({type(error).__name__}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
