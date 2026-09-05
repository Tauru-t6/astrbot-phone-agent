#!/usr/bin/env bash
set -euo pipefail
umask 077

ROOT_DIR="${XIAOMI_SYNC_DIR:-/home/tauru/data/xiaomi_health_sync}"
DATA_DIR="${HEALTH_DATA_DIR:-$ROOT_DIR/data}"
PYTHON="${XIAOMI_SYNC_PYTHON:-$ROOT_DIR/.venv/bin/python}"
DAYS="${HEALTH_SYNC_DAYS:-7}"

if [[ ! -x "$PYTHON" ]]; then
  echo "xiaomi-sync Python interpreter not found: $PYTHON" >&2
  exit 1
fi
if ! [[ "$DAYS" =~ ^[0-9]+$ ]] || (( DAYS < 1 || DAYS > 90 )); then
  echo "HEALTH_SYNC_DAYS must be an integer from 1 to 90" >&2
  exit 2
fi

mkdir -p "$DATA_DIR"
cd "$ROOT_DIR"
export HEALTH_DATA_DIR="$DATA_DIR"
"$PYTHON" -m health_vault.cli sync --days "$DAYS"

if [[ -f "$DATA_DIR/token.json" ]]; then
  chmod 600 "$DATA_DIR/token.json"
fi
