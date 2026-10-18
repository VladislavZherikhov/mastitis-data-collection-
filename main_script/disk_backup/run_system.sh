#!/usr/bin/env bash
# Обёртка для запуска из папки disk_backup/ — делегирует единому run_system.sh в корне проекта.
set -euo pipefail
cd "$(dirname "$0")/.."
exec ./run_system.sh "$@"
