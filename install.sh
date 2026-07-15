#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
exec python3 "$REPO_ROOT/scripts/install_local.py" "$@"
