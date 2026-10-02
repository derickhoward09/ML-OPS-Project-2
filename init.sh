#!/usr/bin/env bash
# Configure integrations and install scheduler jobs on linux.wpi.edu.
set -Eeuo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$REPO_ROOT/scripts/resumelens_scheduler.py" init "$@"
