#!/usr/bin/env bash
# Run on the VM after cloning: chmod +x scripts/setup_server.sh && ./scripts/setup_server.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE}")/.." && pwd)"
cd "$REPO_DIR"

echo "[VoltEdge] Creating virtualenv..."
python3 -m venv .venv
source .venv/bin/activate

echo "[VoltEdge] Upgrading pip..."
pip install --upgrade pip

echo "[VoltEdge] Installing dependencies..."
pip install -r requirements.txt

echo "[VoltEdge] Ensuring logs/ and data/ directories exist..."
mkdir -p logs data

echo "[VoltEdge] Setup complete."
