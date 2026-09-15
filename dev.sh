#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

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

# --- Cleanup on exit ---
cleanup() {
  echo ""
  echo "Shutting down..."
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  echo "Done."
}
trap cleanup EXIT INT TERM

# --- Start backend ---
echo "Starting backend..."
source "$ROOT_DIR/.venv/bin/activate"

# Adjust this command for your framework
# Examples:
#   uvicorn src.main:app --reload --host 0.0.0.0 --port 8000
#   python -m src.main
#   flask run --debug --port 8000
uvicorn src.main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

echo ""
echo "  Backend: http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop."

wait
