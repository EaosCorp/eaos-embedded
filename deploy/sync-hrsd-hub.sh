#!/bin/sh
# Sync this checkout onto the HRSD Nansemond hub and restart the services.
#
#   deploy/sync-hrsd-hub.sh                 # sync -> install --site hrsd-nans -> restart -> doctor
#   DRY=1 deploy/sync-hrsd-hub.sh           # show what would change, touch nothing
#   deploy/sync-hrsd-hub.sh other-host      # another hub with the same layout
#
# The box is on cellular: tests, docs, the vision piece and media stay home.
# Replaced files are kept under /opt/uii.bak-<UTC stamp>/ on the box; nothing in
# /etc/uii or /var/lib/uii is touched. SSH is Tailscale SSH (user pi, passwordless sudo).
set -eu
SRC="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${1:-hrsd-rpi-01}"
DEST=/opt/uii
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

rsync -a --no-owner --no-group --rsync-path="sudo rsync" \
    --exclude .git --exclude __pycache__ --exclude '.pytest_cache' \
    --exclude '._*' --exclude .DS_Store \
    --exclude tests --exclude docs --exclude '*.md' \
    --exclude uii_vision --exclude deploy/units --exclude deploy/sync-dcwa-edge.sh \
    --backup --backup-dir="$DEST.bak-$STAMP" \
    ${DRY:+--dry-run --itemize-changes} \
    "$SRC/" "$HOST:$DEST/"
[ -n "${DRY:-}" ] && { echo "(dry run; nothing changed)"; exit 0; }

ssh "$HOST" "sudo sh $DEST/deploy/install.sh --site hrsd-nans \
  && sudo systemctl restart uii-hub uii-mqtt-shim \
  && sleep 4 \
  && UII_TOKEN=\"\$(grep -oE 'UII_TOKEN=.*' ~/.bashrc | head -1 | cut -d= -f2- | tr -d \"\\\"'\")\" uii doctor"
