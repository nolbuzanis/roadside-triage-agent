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
NGROK_DOMAIN="tattoo-margarine-demanding.ngrok-free.dev"
NGROK_URL="https://$NGROK_DOMAIN"
echo "Starting ngrok tunnel on $NGROK_DOMAIN..."
ngrok http "$PORT" --domain="$NGROK_DOMAIN" --log=stdout &
NGROK_PID=$!

echo "Waiting for ngrok tunnel..."
for i in $(seq 1 30); do
  if curl -sf "https://$NGROK_DOMAIN" -o /dev/null 2>/dev/null; then
    break
  fi
  sleep 0.5
done

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
