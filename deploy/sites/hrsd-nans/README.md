# Site overlay: HRSD Nansemond hub (`hrsd-nans-rpiplc-01`)

Applied with `sudo ./deploy/install.sh --site hrsd-nans` (config files only if absent;
`network.sh` always). Tokens are generated on the box and live only in `/etc/uii/hub.json`;
`hub.json` here ships with empty credentials and must never be copied over a live file.

## Topology (as found 2026-09-04)

| Interface | Address | What is on it |
|---|---|---|
| eth0 (native Pi port) | 10.10.10.20/24 **+ 192.168.1.20/24** | the plant-side LAN: NH4MOD unit at 192.168.1.50 (its own mosquitto on 1883), a PC at 192.168.1.110 |
| eth1 (W5100 on SPI) | 10.10.11.20/24 | the intended instrument LAN; nothing plugged in |
| tailscale0 | 100.114.30.40 | management (Tailscale SSH, user `pi`) |
| ppp0 | cellular | the only internet; keep transfers lean |

The legacy unit keeps its own broker (gateway default `mqtt_host=127.0.0.1`), so the shim
subscribes to `192.168.1.50:1883` and the hub's own mosquitto is idle for this unit. The
unit publishes as **pi_id `nh4mod`**; the shim's unit key must match.

Known unit-side issues (need the unit's login, ask Arba): default gateway set to
`255.255.255.0`, clock frozen at 2026-07-08 (no time source). The hub stamps evidence with
its own clock; the unit's `ts` rides inside payloads and shows as `clock_skew_s` in health.

## Files

- `mqtt-shim.json` — broker = the unit, key `nh4mod`, 90 s offline, 15 s ack timeout.
- `hub.json` — roles/detections as on the box, credentials empty (see above).
- `mosquitto-uii.conf` — hub-box broker bound to loopback + eth1 (for a unit repointed here).
- `network.sh` — ensures eth0-1 carries 192.168.1.20/24 (ARP-checks first; idempotent).

Verify after any change: `uii doctor`.
