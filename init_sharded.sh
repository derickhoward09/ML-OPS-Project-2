#!/usr/bin/env bash
# Set up or manage the three-scheduler mode. Run preflight concurrently on all nodes.
set -Eeuo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$ROOT/sharded/cluster.py" "$@"
