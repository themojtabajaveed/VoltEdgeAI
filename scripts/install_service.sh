#!/usr/bin/env bash
# Usage example: sudo ./scripts/install_service.sh mujtaba12cr
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <linux-username>"
  exit 1
fi

USERNAME="$1"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE}")/.." && pwd)"

SERVICE_SRC="$REPO_DIR/scripts/voltedge.service.example"
SERVICE_DST="/etc/systemd/system/voltedge.service"

echo "[VoltEdge] Installing systemd service for user $USERNAME"

sudo sed "s|/home/USERNAME/voltedge|/home/$USERNAME/voltedge|g" "$SERVICE_SRC" | sudo tee "$SERVICE_DST" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable voltedge.service
sudo systemctl restart voltedge.service

echo "[VoltEdge] Service installed and started. Check status with: sudo systemctl status voltedge.service"
