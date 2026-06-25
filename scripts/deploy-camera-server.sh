#!/usr/bin/env bash
# Deploy the canonical camera-server (this repo's tools/camera-server) to a CSI
# HIL host's /home/pi/hil-camera-server, so the deployment TRACKS THE REPO
# instead of drifting as untracked in-place edits.
#
#   scripts/deploy-camera-server.sh [pi@]<host>     # e.g. rpi-hil006
#
# Syncs runtime code only — server.py, backends/, illuminators/, tuning/,
# __init__.py — and deliberately NOT:
#   * README.md            (repo docs)
#   * hil-camera.service   (the systemd unit is HOST-SPECIFIC: e.g. rpi-hil006
#                           runs with `--no-neopixel` because it has no NeoPixel
#                           ring. Manage the unit per host; don't overwrite it.)
#
# It backs up the existing deployment, untars the new code over it, restarts
# hil-camera.service, and prints /health. Run from anywhere that can SSH the
# host (the controller reaches rpi-hil006 directly).
set -euo pipefail

HOST="${1:?usage: deploy-camera-server.sh [pi@]<host>}"
[[ "$HOST" == *@* ]] || HOST="pi@$HOST"
SRC="$(cd "$(dirname "$0")/.." && pwd)/tools/camera-server"
DEST=/home/pi/hil-camera-server
[ -f "$SRC/server.py" ] || { echo "ERROR: $SRC/server.py not found — run from the repo" >&2; exit 1; }

echo "deploy $SRC -> $HOST:$DEST"
ssh "$HOST" "if [ -d '$DEST' ]; then cp -r '$DEST' '$DEST.bak.'\$(date +%s); echo backed-up; else mkdir -p '$DEST'; fi"
tar -C "$SRC" -cf - \
    --exclude=__pycache__ --exclude='*.pyc' --exclude=README.md --exclude=hil-camera.service . \
  | ssh "$HOST" "tar -C '$DEST' -xf -"
echo "restarting hil-camera.service"
ssh "$HOST" "sudo systemctl restart hil-camera.service && sleep 3 && systemctl is-active hil-camera.service"
echo "health:"
ssh "$HOST" "curl -fsS http://localhost:8080/health" && echo || echo "(health check failed — inspect the host)"
echo "done."
