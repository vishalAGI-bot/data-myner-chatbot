#!/usr/bin/env bash
# One-time setup: Python virtual environment + backend packages + frontend packages.
# Run WITHOUT sudo:   bash setup.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [ "$(id -u)" -eq 0 ]; then
  echo "Please run without sudo (sudo hides your Python/conda and makes files owned by root)."; exit 1
fi
PY="${PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PY" ] || { echo "Python 3.10+ not found. Install it (or activate conda) and try again."; exit 1; }
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), sys.version' \
  || { echo "Python 3.10 or newer is required (found: $("$PY" --version))."; exit 1; }

cd "$ROOT/backend"
if [ ! -d .venv ]; then
  echo "Creating backend/.venv with $("$PY" --version)"
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
[ -f .env ] || { cp .env.example .env; echo "Created backend/.env - fill in the SSH and database settings."; }

cd "$ROOT/frontend"
command -v npm >/dev/null || { echo "Node.js 18+ (npm) not found. Install Node.js and run setup again."; exit 1; }
npm install --no-fund --no-audit

echo
echo "Setup done. Next:"
echo "  1. Edit backend/.env (ANALYST_SOURCE=postgres + SSH/DB settings), or keep ANALYST_SOURCE=snapshot"
echo "     and unzip public.zip into backend/data/public for offline mode."
echo "  2. Check the database:   cd backend && .venv/bin/python -m analyst.tools.check"
echo "  3. Start everything:     bash start.sh"
