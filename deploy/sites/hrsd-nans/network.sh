#!/bin/sh
# Ensure eth0 (NetworkManager connection eth0-1) carries 192.168.1.20/24 so the hub
# can reach the NH4MOD unit's broker at 192.168.1.50. Idempotent; ARP-checks the
# address before claiming it the first time. Run as root.
set -eu
CON=eth0-1
ADDR=192.168.1.20
IFACE=eth0
command -v nmcli >/dev/null 2>&1 || { echo "network.sh: nmcli not found; skipping" >&2; exit 0; }
if nmcli -g ipv4.addresses con show "$CON" 2>/dev/null | tr ',' '\n' | grep -q "^ *$ADDR/"; then
    echo "      $CON already has $ADDR/24"
    exit 0
fi
python3 - "$IFACE" "$ADDR" <<'PY' || { echo "network.sh: $ADDR is in use on $IFACE; not adding" >&2; exit 0; }
import socket, struct, sys, time
iface, target = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(0x0806)); s.bind((iface, 0)); s.settimeout(0.5)
mac = s.getsockname()[4]
pkt = (b'\xff'*6 + mac + b'\x08\x06' + struct.pack('!HHBBH', 1, 0x0800, 6, 4, 1)
       + mac + socket.inet_aton('0.0.0.0') + b'\x00'*6 + socket.inet_aton(target))
for _ in range(3):
    s.send(pkt); end = time.time() + 1.0
    while time.time() < end:
        try: f = s.recv(2048)
        except socket.timeout: break
        if len(f) >= 42 and struct.unpack('!H', f[20:22])[0] == 2 and f[28:32] == socket.inet_aton(target):
            sys.exit(1)
PY
nmcli con mod "$CON" +ipv4.addresses "$ADDR/24"
nmcli con up "$CON" >/dev/null
echo "      added $ADDR/24 to $CON"
