#!/usr/bin/env bash
# Cross-platform start script for the Prototype Feedback ingest server.
# Works on macOS, Linux, and WSL/Git-Bash on Windows.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  PY="python"
fi
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3.10+ is required and not on PATH." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  "$PY" -m venv .venv
fi

# Pick the venv interpreter (path differs on Windows vs POSIX)
if [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
else
  echo "Could not find venv python." >&2
  exit 1
fi

"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r requirements.txt

HOST="${PFB_HOST:-127.0.0.1}"
PORT="${PFB_PORT:-8090}"
echo "Starting Prototype Feedback server at http://${HOST}:${PORT}"
exec "$VENV_PY" -m uvicorn main:app --host "$HOST" --port "$PORT" --reload
