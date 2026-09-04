#!/bin/sh
# UII install for a hub box or a module Pi. Idempotent; safe to re-run for upgrades.
#
#   sudo ./deploy/install.sh                       # software only (sim runs anywhere)
#   sudo ./deploy/install.sh --with-hw             # + pyserial & ADS1115 libs (module Pi)
#   sudo ./deploy/install.sh --site hrsd-nans      # + the per-site overlay (deploy/sites/NAME)
#
# Installs to /opt/uii, config in /etc/uii, evidence in /var/lib/uii.
# Existing /etc/uii and /etc/mosquitto/conf.d files are NEVER overwritten;
# a site's network.sh is always run (it is idempotent by contract).
set -eu

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/uii
WITH_HW=0
SITE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --with-hw) WITH_HW=1 ;;
        --site) SITE="$2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 64 ;;
    esac
    shift
done
SITE_DIR=""
if [ -n "$SITE" ]; then
    SITE_DIR="$SRC/deploy/sites/$SITE"
    [ -d "$SITE_DIR" ] || { echo "no such site overlay: $SITE_DIR" >&2; exit 66; }
fi

echo "[1/6] code -> $DEST"
mkdir -p "$DEST"
if [ "$SRC" = "$DEST" ]; then
    echo "      (already running from $DEST)"
elif command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude .git --exclude data --exclude __pycache__ \
        "$SRC/" "$DEST/"
else
    cp -R "$SRC/." "$DEST/"
fi
chmod +x "$DEST/deploy/install.sh" "$DEST/deploy/mosquitto/wait-for-listener-addrs.sh" 2>/dev/null || true

echo "[2/6] config -> /etc/uii (existing files kept)"
mkdir -p /etc/uii /var/lib/uii
put_if_absent() {   # put_if_absent <src> <dest>
    if [ -f "$2" ]; then echo "      keep $2"; else cp "$1" "$2"; echo "      new  $2 (from $1)"; fi
}
if [ -n "$SITE_DIR" ]; then
    for f in hub.json mqtt-shim.json pimod.env; do
        [ -f "$SITE_DIR/$f" ] && put_if_absent "$SITE_DIR/$f" "/etc/uii/$f"
    done
fi
put_if_absent "$DEST/deploy/hub.json.example"       /etc/uii/hub.json
put_if_absent "$DEST/deploy/pimod.env.example"      /etc/uii/pimod.env
put_if_absent "$DEST/deploy/mqtt-shim.json.example" /etc/uii/mqtt-shim.json

echo "[3/6] cli -> /usr/local/bin/uii"
cat > /usr/local/bin/uii <<'WRAP'
#!/bin/sh
PYTHONPATH=/opt/uii exec python3 -m uii.cli "$@"
WRAP
chmod +x /usr/local/bin/uii

if [ "$WITH_HW" = 1 ]; then
    echo "[4/6] hardware libraries (pyserial, ADS1115)"
    pip3 install --break-system-packages pyserial adafruit-circuitpython-ads1x15 \
        || pip3 install pyserial adafruit-circuitpython-ads1x15
else
    echo "[4/6] skipping hardware libraries (use --with-hw on a module Pi)"
fi

echo "[5/6] mosquitto (hub-box broker, only if mosquitto is installed)"
if command -v mosquitto >/dev/null 2>&1 && [ -d /etc/mosquitto/conf.d ]; then
    if [ -n "$SITE_DIR" ] && [ -f "$SITE_DIR/mosquitto-uii.conf" ]; then
        put_if_absent "$SITE_DIR/mosquitto-uii.conf" /etc/mosquitto/conf.d/uii.conf
    else
        put_if_absent "$DEST/deploy/mosquitto/uii.conf.example" /etc/mosquitto/conf.d/uii.conf
    fi
    mkdir -p /etc/systemd/system/mosquitto.service.d
    cp "$DEST/deploy/mosquitto/10-wait-for-listener-addrs.conf" \
       /etc/systemd/system/mosquitto.service.d/10-wait-for-listener-addrs.conf
    echo "      drop-in installed: mosquitto waits for its listener addresses"
else
    echo "      mosquitto not installed — skipped (units that keep their own broker need none)"
fi

echo "[6/6] systemd units"
if command -v systemctl >/dev/null 2>&1; then
    cp "$DEST/deploy/uii-hub.service" "$DEST/deploy/uii-pimod.service" \
        "$DEST/deploy/uii-mqtt-shim.service" /etc/systemd/system/
    systemctl daemon-reload
    if [ -n "$SITE_DIR" ] && [ -f "$SITE_DIR/network.sh" ]; then
        echo "      site network: $SITE_DIR/network.sh"
        sh "$SITE_DIR/network.sh"
    fi
    echo
    echo "Installed. Review /etc/uii/hub.json and /etc/uii/mqtt-shim.json, then:"
    echo "  hub box with legacy units:   sudo systemctl enable --now uii-hub uii-mqtt-shim"
    echo "  module Pi (native agent):    sudo systemctl enable --now uii-hub uii-pimod"
    echo "  then:                        uii doctor      # services, brokers, heard vs configured"
else
    echo "no systemd — run manually:"
    echo "  UII_CONFIG=/etc/uii/hub.json python3 -m uii.hub.main"
    echo "  UII_SHIM_CONFIG=/etc/uii/mqtt-shim.json python3 -m uii_analyzer.mqtt_shim"
fi
