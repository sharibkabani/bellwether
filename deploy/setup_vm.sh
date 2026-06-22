#!/usr/bin/env bash
# Run this ON the Oracle VM after the project has been copied to
# /home/ubuntu/bellwether. Idempotent — safe to re-run.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"
echo "Setting up Bellwether in $PROJECT_DIR"

sudo apt-get update -y
sudo apt-get install -y python3-venv python3-pip

# Fresh venv (the Mac's .venv is the wrong architecture, never copy it).
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Quick smoke check that the package imports and tests pass.
.venv/bin/python -m pytest -q || echo "WARNING: tests reported failures — review before going live."

echo
echo "Setup complete. Confirm .env and config.yaml are present, then:"
echo "  sudo cp deploy/bellwether.service /etc/systemd/system/bellwether.service"
echo "  sudo systemctl daemon-reload && sudo systemctl enable --now bellwether"
echo "  journalctl -u bellwether -f"
