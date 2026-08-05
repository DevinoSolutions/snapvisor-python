#!/usr/bin/env bash
# Thin wrapper around scripts/regen.py. All arguments are forwarded, e.g.
#   ./scripts/regen.sh --offline
#   ./scripts/regen.sh --check
set -euo pipefail
exec python "$(dirname "$0")/regen.py" "$@"
