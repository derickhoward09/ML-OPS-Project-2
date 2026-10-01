#!/usr/bin/env bash
# Configure the ordinary once-a-minute Cowrie monitor and Healthchecks.
set -Eeuo pipefail
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec /bin/bash "$REPO_ROOT/cowrie/init_common.sh" minute "$@"
