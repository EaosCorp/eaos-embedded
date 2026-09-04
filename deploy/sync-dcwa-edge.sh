#!/bin/sh
# Sync this checkout onto the DC Water Blue Plains edge boxes and restart uii-vision.
#
#   deploy/sync-dcwa-edge.sh                    # both boxes (dcwa-001-root dcwa-002-root)
#   deploy/sync-dcwa-edge.sh dcwa-002-root      # one box
#   DRY=1 deploy/sync-dcwa-edge.sh              # show what would change, no restart
#
# The boxes were installed by hand (2026-08-07) with THIS layout, which we keep
# so the systemd unit (`python3 -m extensions.vision.edge`) stays valid:
#
#   repo uii/           -> /opt/eaos/uii-analyzer/uii/
#   repo uii_vision/    -> /opt/eaos/uii-analyzer/extensions/vision/
#   repo uii_analyzer/  -> /opt/eaos/uii-analyzer/extensions/analyzer/
#   repo extensions/*   -> /opt/eaos/uii-analyzer/extensions/*
#
# (`uii/hub/main.py` resolves an extension as `uii_<name>` first and falls back
# to `extensions.<name>`, so the same code runs in either layout.) Docstrings
# on the box will read `uii_vision.*` where the earlier hand-copy said
# `extensions.vision.*` — cosmetic.
#
# Never touched: /etc/eaos/* (creds, tokens), /var/lib/eaos/* (frames, evidence),
# the systemd units and drop-ins. Replaced files are kept under
# /opt/eaos/uii-analyzer.bak-<UTC stamp>/ on the box. Nothing is deleted.
#
# SSH is Tailscale SSH on the eaos.ai tailnet: `tailscale switch eaos.ai` first.
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/eaos/uii-analyzer
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
HOSTS="${*:-dcwa-001-root dcwa-002-root}"

rs() {   # rs <src-dir/> <host> <dest-dir/>
    rsync -a --no-owner --no-group \
        --exclude __pycache__ --exclude '._*' --exclude .DS_Store \
        --exclude tests --exclude docs --exclude '*.md' \
        --backup --backup-dir="$DEST.bak-$STAMP" \
        ${DRY:+--dry-run --itemize-changes} \
        "$1" "$2:$3"
}

for h in $HOSTS; do
    echo "== $h"
    ssh "$h" "mkdir -p $DEST/uii $DEST/extensions/vision $DEST/extensions/analyzer"
    rs "$SRC/uii/"          "$h" "$DEST/uii/"
    rs "$SRC/uii_vision/"   "$h" "$DEST/extensions/vision/"
    rs "$SRC/uii_analyzer/" "$h" "$DEST/extensions/analyzer/"
    for x in authority detections exports faceplate scheduler; do
        rs "$SRC/extensions/$x/" "$h" "$DEST/extensions/$x/"
    done
    rs "$SRC/extensions/__init__.py" "$h" "$DEST/extensions/"
    if [ -n "${DRY:-}" ]; then
        echo "   (dry run — no restart)"
        continue
    fi
    ssh "$h" "cd $DEST && find . -name '._*' -delete && python3 -m compileall -q uii extensions >/dev/null \
        && systemctl restart uii-vision.service && sleep 3 \
        && systemctl is-active uii-vision.service \
        && curl -sf -m 5 http://127.0.0.1:8400/.well-known/uii >/dev/null \
        && echo '   uii-vision restarted, API answering' \
        && journalctl -u uii-vision --since '-30 sec' --no-pager | grep -m1 'hub up' || true"
done

cat <<EOF

Verify from the laptop (viewer token in vault dcwa-uii-api-tokens.md):
  curl -sI -H "Authorization: Bearer \$V" http://100.124.44.125:8400/v1/modules/campod-01/frame
  -> HTTP/1.1 200, Content-Type: image/jpeg, Cache-Control: no-store, X-Frame-Hash, X-Frame-Time
EOF
