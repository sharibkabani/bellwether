#!/usr/bin/env bash
# One-time bootstrap to enable push-to-deploy on the Oracle VM.
#
# Run this ON the box after the bot service from ORACLE.md §4 is already running:
#   cd ~/bellwether && git pull && bash deploy/bootstrap.sh
#
# It installs the auto-update timer (checks GitHub every 2 min, deploys new
# commits, restarts the bot) and the `bw` helper, then applies the current code.
# Idempotent — safe to re-run. Uses sudo for the systemd bits.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="bellwether"

echo "Bootstrapping push-to-deploy from $DIR"

# --- preflight: must be a git clone with a reachable upstream ----------------
if ! git -C "$DIR" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    echo "ERROR: no upstream branch set in $DIR."
    echo "       Auto-update needs a git clone (not an scp copy). Clone the repo, e.g.:"
    echo "         git clone https://github.com/sharibkabani/bellwether.git ~/bellwether"
    exit 1
fi

# --- preflight: the bot service should exist (install it from ORACLE.md §4) ---
if ! systemctl list-unit-files | grep -q "^${SERVICE}.service"; then
    echo "WARNING: ${SERVICE}.service is not installed yet."
    echo "         Install it first (ORACLE.md §4):"
    echo "           sudo cp deploy/${SERVICE}.service /etc/systemd/system/"
    echo "           sudo systemctl daemon-reload && sudo systemctl enable --now ${SERVICE}"
fi

chmod +x "$DIR/deploy/autoupdate.sh" "$DIR/deploy/bw"

# --- install the auto-update timer -------------------------------------------
echo "Installing the auto-update timer…"
sudo cp "$DIR/deploy/bellwether-update.service" "$DIR/deploy/bellwether-update.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bellwether-update.timer

# --- install the `bw` helper on PATH -----------------------------------------
echo "Installing the bw helper at /usr/local/bin/bw…"
sudo ln -sf "$DIR/deploy/bw" /usr/local/bin/bw
if alias bw >/dev/null 2>&1; then
    echo "NOTE: a shell 'alias bw=...' is shadowing the new command — remove it from your shell rc."
fi

# --- apply the current checkout now ------------------------------------------
if systemctl list-unit-files | grep -q "^${SERVICE}.service"; then
    echo "Restarting ${SERVICE} to apply the current code…"
    sudo systemctl restart "$SERVICE"
fi

echo
echo "Done. Push-to-deploy is live."
echo "  • Push on your Mac → the box deploys within ~2 min."
echo "  • Watch it land:    bw watch"
echo "  • Deploy now:       bw update"
echo "  • Timer status:     systemctl status bellwether-update.timer"
