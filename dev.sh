#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8000

# --- Preflight checks ---
if [ ! -f "$ROOT_DIR/.env" ]; then
  echo "Error: .env file not found."
  echo "Copy .env.example to .env and fill in the required values."
  exit 1
fi

if [ ! -d "$ROOT_DIR/.venv" ]; then
  echo "Error: Python virtual environment not found at .venv/"
  echo "Create one with: python3 -m venv .venv"
  exit 1
fi

if ! command -v ngrok &> /dev/null; then
  echo "Error: ngrok not installed."
  echo "Install with: brew install ngrok"
  exit 1
fi

# --- Cleanup on exit ---
cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" 2>/dev/null || true
  kill "$NGROK_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  echo "Done."
}
trap cleanup EXIT INT TERM

# --- Start backend ---
echo "Starting backend..."
source "$ROOT_DIR/.venv/bin/activate"
uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT" &
BACKEND_PID=$!

# Wait for health endpoint
echo "Waiting for backend..."
until curl -sf "http://localhost:$PORT/health" > /dev/null 2>&1; do
  sleep 0.5
done
echo "Backend ready at http://localhost:$PORT"

# --- Start ngrok ---
echo "Starting ngrok tunnel..."
ngrok http "$PORT" --log=stdout &
NGROK_PID=$!

echo "Waiting for ngrok tunnel..."
NGROK_URL=""
for i in $(seq 1 30); do
  NGROK_URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['tunnels'][0]['public_url'])" 2>/dev/null) || true
  if [ -n "$NGROK_URL" ]; then
    break
  fi
  sleep 0.5
done

if [ -z "$NGROK_URL" ]; then
  echo "Error: ngrok tunnel failed to establish."
  exit 1
fi

echo ""
echo "========================================="
echo "  Backend:  http://localhost:$PORT"
echo "  ngrok:    $NGROK_URL"
echo "  Health:   $NGROK_URL/health"
echo "========================================="
echo ""
echo "Set your Twilio voice webhook to:"
echo "  $NGROK_URL/api/v1/twilio/voice"
echo ""
echo "Press Ctrl+C to stop."

wait
