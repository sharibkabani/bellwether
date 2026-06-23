#!/usr/bin/env bash
# Pull-based push-to-deploy for Bellwether.
#
# Run periodically by ``bellwether-update.timer``. Checks the git remote; when a
# new commit is on the tracked branch it fast-forwards, reinstalls deps only if
# they changed, and restarts the bot so the new code takes effect. Stays SILENT
# when there's nothing new, so ``bw watch`` only shows activity on a real deploy.
#
# Runs as root (the timer's service has no User=), but all git/pip operations are
# done AS THE REPO OWNER via ``sudo -u`` so file ownership stays correct and git
# needs no ``safe.directory`` exception. The service restart is done as root.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER="$(stat -c '%U' "$DIR")"
SERVICE="bellwether"

as_owner() { sudo -u "$OWNER" -H "$@"; }

cd "$DIR"

# Need a tracked upstream (i.e. the repo was cloned, not scp'd). Bail clearly.
if ! as_owner git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    echo "bellwether-update: no upstream branch set — skipping (clone the repo to enable auto-update)"
    exit 0
fi

before="$(as_owner git rev-parse HEAD)"
as_owner git fetch --quiet origin
remote="$(as_owner git rev-parse '@{u}')"

# Up to date → exit quietly (keeps the log clean between deploys).
[ "$before" = "$remote" ] && exit 0

echo "bellwether-update: new commit detected ${remote:0:7} (was ${before:0:7}) — deploying"
as_owner git merge --ff-only --quiet "$remote"

# Reinstall dependencies only when requirements.txt actually changed.
if ! as_owner git diff --quiet "$before" HEAD -- requirements.txt; then
    echo "bellwether-update: requirements.txt changed — reinstalling dependencies"
    as_owner "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"
fi

echo "bellwether-update: restarting $SERVICE at $(as_owner git rev-parse --short HEAD)"
systemctl restart "$SERVICE"
echo "bellwether-update: deploy complete"
