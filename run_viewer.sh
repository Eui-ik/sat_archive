#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/Caskroom/miniconda/base/bin/python3}"
DATA_DIR="${DATA_DIR:-sentinel1_jeju}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"

exec "$PYTHON_BIN" sentinel_viewer/app.py \
  --data-dir "$DATA_DIR" \
  --host "$HOST" \
  --port "$PORT"
