#!/usr/bin/env bash
# Linux/macOS launcher: backend on :8001, frontend dev server on :3000
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ ! -d "$ROOT/backend/.venv" ]; then
  python3 -m venv "$ROOT/backend/.venv"
fi
"$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
[ -d "$ROOT/frontend/node_modules" ] || (cd "$ROOT/frontend" && npm install)
(cd "$ROOT/backend" && "$ROOT/backend/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8001) &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
cd "$ROOT/frontend" && npm run dev
