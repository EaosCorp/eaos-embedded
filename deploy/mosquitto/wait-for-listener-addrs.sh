#!/bin/sh
# Wait (<= 30 s) until every `listener PORT ADDR` address in the given
# mosquitto conf exists on some interface. Loopback / wildcard skipped.
# Always exits 0: a missing address is mosquitto's problem to report.
CONF="${1:-/etc/mosquitto/conf.d/uii.conf}"
[ -r "$CONF" ] || exit 0
ADDRS=$(awk '$1=="listener" && NF>=3 {print $3}' "$CONF" \
        | grep -vE '^(127\.|0\.0\.0\.0$|::$|localhost$)' | sort -u)
[ -n "$ADDRS" ] || exit 0
i=0
while [ $i -lt 30 ]; do
    missing=""
    for a in $ADDRS; do
        ip -o addr show 2>/dev/null | grep -q " $a/" || missing="$missing $a"
    done
    [ -z "$missing" ] && exit 0
    i=$((i+1)); sleep 1
done
echo "wait-for-listener-addrs: still missing:$missing after 30 s; starting anyway" >&2
exit 0
