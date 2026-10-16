#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
exec python3 test_backup.py "$@"
