#!/usr/bin/env bash
# Start the Data analyst (8200), the live Journey agent (8100) and the UI (5173).
# Run WITHOUT sudo:   bash start.sh        Ctrl+C stops everything.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"
if [ "$(id -u)" -eq 0 ]; then
  echo "Please run without sudo:  bash start.sh"; exit 1
fi

# Python: backend/.venv first, then $PYTHON, python3, python.
if [ -x "$ROOT/backend/.venv/bin/python" ]; then PY="$ROOT/backend/.venv/bin/python"
else PY="${PYTHON:-$(command -v python3 || command -v python || true)}"; fi
[ -n "$PY" ] || { echo "Python not found. Run: bash setup.sh"; exit 1; }
if ! "$PY" -c "import fastapi, uvicorn, psycopg, duckdb, sqlglot, paramiko, yaml" 2>/dev/null; then
  echo "Backend packages are missing for $PY."
  echo "Run once:  bash setup.sh      (or: $PY -m pip install -r backend/requirements.txt)"
  exit 1
fi

cd "$ROOT/backend"
[ -f .env ] || { cp .env.example .env; echo "Created backend/.env from the example (offline snapshot mode)."; }
"$PY" -m uvicorn analyst.api:app --port 8200 > "$LOGS/analyst.log" 2>&1 &
ANALYST=$!
"$PY" -m uvicorn journey_agent.api:app --port 8100 > "$LOGS/journey.log" 2>&1 &
JOURNEY=$!
trap 'kill $ANALYST $JOURNEY 2>/dev/null' EXIT

# Wait until both APIs answer (or show why they did not start).
for name in analyst:8200 journey:8100; do
  port="${name#*:}"; label="${name%%:*}"; ok=""
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null "http://127.0.0.1:$port/api/health"; then ok=1; break; fi
    sleep 0.5
  done
  if [ -n "$ok" ]; then echo "✓ $label API running on port $port"
  else echo "✗ $label API did not start. Last lines of logs/$label.log:"; tail -n 20 "$LOGS/$label.log"; exit 1; fi
done
curl -s "http://127.0.0.1:8200/api/health" | "$PY" -c "import json,sys; d=json.load(sys.stdin); print('  data:', 'ready' if d.get('data_ready') else 'NOT ready -', d.get('source')); print('  model:', d.get('llm'))" || true

cd "$ROOT/frontend"
[ -d node_modules ] || npm install --no-fund --no-audit
echo "Open http://localhost:5173   (logs in ./logs, Ctrl+C to stop)"
echo "Connect your live PostgreSQL: sidebar -> Connect live database (Settings page)"
npm run dev
