#!/bin/sh
# UII install for the Pi. Idempotent; safe to re-run for upgrades.
#
#   sudo ./deploy/install.sh              # software only (sim runs anywhere)
#   sudo ./deploy/install.sh --with-hw    # + pyserial & ADS1115 libs (real Pi)
#
# Installs to /opt/uii, config in /etc/uii, evidence in /var/lib/uii.
# Existing /etc/uii files are NEVER overwritten.
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/uii

echo "[1/5] code -> $DEST"
mkdir -p "$DEST"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude .git --exclude data --exclude __pycache__ \
        "$SRC/" "$DEST/"
else
    cp -R "$SRC/." "$DEST/"
fi

echo "[2/5] config -> /etc/uii (existing files kept)"
mkdir -p /etc/uii /var/lib/uii
[ -f /etc/uii/hub.json ]  || cp "$DEST/deploy/hub.json.example"  /etc/uii/hub.json
[ -f /etc/uii/pimod.env ] || cp "$DEST/deploy/pimod.env.example" /etc/uii/pimod.env
[ -f /etc/uii/mqtt-shim.json ] || cp "$DEST/deploy/mqtt-shim.json.example" /etc/uii/mqtt-shim.json

echo "[3/5] cli -> /usr/local/bin/uii"
cat > /usr/local/bin/uii <<'EOF'
#!/bin/sh
PYTHONPATH=/opt/uii exec python3 -m uii.cli "$@"
EOF
chmod +x /usr/local/bin/uii

if [ "${1:-}" = "--with-hw" ]; then
    echo "[4/5] hardware libraries (pyserial, ADS1115)"
    pip3 install --break-system-packages pyserial adafruit-circuitpython-ads1x15 \
        || pip3 install pyserial adafruit-circuitpython-ads1x15
else
    echo "[4/5] skipping hardware libraries (use --with-hw on the real Pi)"
fi

echo "[5/5] systemd units"
if command -v systemctl >/dev/null 2>&1; then
    cp "$DEST/deploy/uii-hub.service" "$DEST/deploy/uii-pimod.service" \
        "$DEST/deploy/uii-mqtt-shim.service" /etc/systemd/system/
    systemctl daemon-reload
    echo
    echo "Installed. Review /etc/uii/pimod.env and /etc/uii/hub.json, then:"
    echo "  sudo systemctl enable --now uii-hub uii-pimod"
    echo "  uii modules        # should show your module OPERATIONAL, mode BRIDGE"
else
    echo "no systemd — run manually:"
    echo "  UII_CONFIG=/etc/uii/hub.json python3 -m uii.hub.main"
    echo "  (env from /etc/uii/pimod.env) python3 -m uii.pimod.main"
fi
