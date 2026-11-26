#!/usr/bin/env bash
# Start the news scanner (creates the venv on first run).
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8787}"
HOST="${HOST:-127.0.0.1}"

if [ ! -x .venv/bin/python ]; then
  echo "· creating virtualenv"
  python3 -m venv .venv
  .venv/bin/pip install -q --disable-pip-version-check --no-cache-dir -r requirements.txt
fi

if [ "${RELOAD:-0}" = "1" ]; then
  exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --reload
else
  exec .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level info
fi
