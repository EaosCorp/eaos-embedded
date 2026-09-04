# HRSD hub: clean start-up, reliable recognition, smooth commands

Plan written 2026-09-04 after the field check on `hrsd-nans-rpiplc-01`. The code on the
box is byte-identical to this working tree (md5-checked), so every fix below lands here
first and is synced out.

**Status (2026-09-04, same day):** phases 0–6 built and tested (`tests/test_mqtt_shim.py`,
11 cases on a real mosquitto); phase 7 waits on the unit's login; phase 8 runs at the next
sync (`deploy/sync-hrsd-hub.sh`).

## What the field check taught us

- The NH4MOD unit is on **eth0** (plant LAN) at **192.168.1.50**, not on eth1 (the instrument
  LAN the deploy pack assumes). eth1 had received zero frames since boot.
- The unit runs **its own mosquitto** (legacy default `mqtt_host=127.0.0.1`) and publishes
  under **pi_id `nh4mod`**. The example shim config says `nh4mod-01`, so even with
  connectivity the shim would have ignored it.
- Fix applied on the box: eth0 carries a second address `192.168.1.20/24` (NetworkManager,
  persistent); `/etc/uii/mqtt-shim.json` points at the unit's broker with key `nh4mod`.
  Adoption took 4 s. `take_control` and `prime` round-tripped with progress and result.
- Hub-side rough edges seen: mosquitto fails twice at every boot (binds 10.10.11.20 before
  the address exists); the hub's own log lines never reach the journal (stdout buffered);
  a 3-minute prime produced ~300 identical PROGRESS envelopes and CLI lines; the unit's
  clock is stuck at 2026-07-08 and it ARPs for a bogus gateway `255.255.255.0`.

## Acceptance (what "works smoothly" means)

1. **Cold boot of the hub**: `systemctl --failed` empty, `uii doctor` PASS, module
   OPERATIONAL within 60 s, no human action.
2. **Unit power-cycle**: BYE within `offline_after_s`, re-adopt within 30 s of the first
   heartbeat, no shim restart needed. Same for a unit broker outage and a hub restart.
3. **Commands**: one ACK, PROGRESS only on change, one RESULT, one journal line per command
   with type, id, outcome, duration. `--watch` prints only changes.
4. **Recognition is checkable**: `uii doctor` compares the configured unit keys with the
   pi_ids actually heard on the broker and says so in words.
5. **Re-imageable**: `deploy/install.sh --site hrsd-nans` reproduces the box, including the
   eth0 address, the shim config and the mosquitto ordering, from the repo alone.

## Phase 0 — Commit what is deployed (S, Keaton commits)

The working tree mixes two workstreams. Split into separate commits so history explains
itself:

1. **Analyzer shim**: `uii_analyzer/mqtt_shim.py`, `uii_analyzer/mqtt_min.py`,
   `tools/fake_legacy_unit.py`, `deploy/uii-mqtt-shim.service`,
   `deploy/mqtt-shim.json.example`, `deploy/install.sh` (+ `chmod +x`),
   `deploy/uii-pimod.service` (ExecStart fix), `tools/legacy-shim.md`,
   `uii_analyzer/README.md`, `uii/hub/api.py`.
2. **Vision**: everything under `uii_vision/`, `deploy/units/`, `deploy/sync-dcwa-edge.sh`,
   `tests/test_vision.py`.
3. **Docs**: `docs/ROADMAP.md` plus this plan.

## Phase 1 — Shim robustness (M)

`uii_analyzer/mqtt_min.py`
- **Half-open detection.** `run()` blocks in `_read_packet` with no timeout; a unit that
  loses power mid-connection leaves the shim hanging until the kernel gives up (minutes).
  Wrap the read in `asyncio.wait_for(..., timeout=keepalive * 1.5)`; a timeout means the
  broker is gone (PINGRESP should have arrived), so drop and reconnect. This is the single
  most important field fix.
- Log connect / disconnect / reconnect lines with the broker address and the reason.
- Guard against QoS>0 PUBLISH (packet-id bytes would land in the payload): read the QoS
  bits from `head` and skip two bytes when set. The legacy gateway is QoS 0 today; this
  keeps a future gateway from silently corrupting payloads.

`uii_analyzer/mqtt_shim.py`
- **Per-unit broker.** Each legacy unit is its own broker, so a second unit (NOX, PO4)
  cannot share the global `broker`. Allow `"broker"` inside a unit entry, defaulting to the
  top-level value; keep one `MQTTClient` per distinct broker.
- **Progress dedupe.** In `_action_status`, forward PROGRESS only when `(pct, message)`
  changed since the last one for that command. Cuts the ~300 envelopes per prime to
  the ~4 real steps. Keep the raw cadence out of the record; the record is for decisions.
- **One line per command** in the journal: `[nh4mod] prime 01a06cdc… succeeded in 191 s`.
- **Clock skew health.** On heartbeat, compute `skew_s = hub_now - unit_ts`; add it to the
  health telem and print a warning once when |skew| > 300 s. Pair with a `health_flag`
  detection in `hub.json.example` so the unit's dead clock is visible on `uii health`.
- **Config validation at start.** Refuse to start with a clear message if a unit entry
  lacks `slot`/`serial`, and print "unit key must equal the unit's PI_ID (legacy default
  `nh4mod`)" in the startup banner. Fix `deploy/mqtt-shim.json.example` to use `nh4mod`.

## Phase 2 — Start-up ordering and units (S)

- `deploy/uii-hub.service`: add `Environment=PYTHONUNBUFFERED=1` so the hub's banner and
  adoption lines reach the journal (today only systemd lines appear). Add
  `After=network-online.target` + `Wants=network-online.target` for symmetry.
- `deploy/uii-mqtt-shim.service`: same network-online ordering (the unit's broker is on an
  eth0 secondary address; today the shim just backs off until it appears, which works but
  hides a real ordering dependency).
- **mosquitto**: ship `deploy/mosquitto/uii.conf` using `bind_interface eth1` instead of a
  literal address, plus `deploy/mosquitto/10-wait-for-network.conf` as a systemd drop-in.
  Either alone fixes the double failure at boot; ship both.
- `deploy/install.sh`: set the executable bit in git; install the mosquitto conf and the
  drop-in when mosquitto is present; make the closing "enable" hint topology-aware:
  gateway-only hub → `uii-hub uii-mqtt-shim`, module Pi → `uii-hub uii-pimod`.

## Phase 3 — Site overlay for hrsd-nans (M)

The eth0 address, the shim config and the broker choice live only on the box today.
Capture them as the per-site overlay the migration playbook already calls for:

```
deploy/sites/hrsd-nans/
  README.md            topology: eth0 = plant LAN (10.10.10.0/24 + 192.168.1.0/24, unit at
                       .50 with its own broker, PC at .110), eth1 = instrument LAN 10.10.11.0/24
                       (empty), cellular ppp0 = tailnet only. Why the shim points at the unit.
  network.sh           idempotent nmcli: ensure eth0-1 has 192.168.1.20/24 (ARP-check first)
  mqtt-shim.json       broker 192.168.1.50:1883, unit nh4mod → slot-1 NH4
  hub.json             the site's roles/detections (credentials stay on the box, never here)
  mosquitto/           uii.conf + drop-in (from Phase 2)
```

- `deploy/install.sh --site hrsd-nans` applies the overlay (config files only if absent,
  network.sh always, units always).
- `deploy/sync-hrsd-hub.sh`, modelled on `sync-dcwa-edge.sh`: rsync the checkout to
  `/opt/uii`, keep a `.bak-<stamp>`, restart `uii-hub uii-mqtt-shim`, then run the doctor.
  Cellular caveat: excludes tests/docs/media, `DRY=1` mode.

## Phase 4 — `uii doctor` (M)

A read-only check that answers "is it recognising the module" in one screen. Add as a CLI
verb (it is an agent surface too, `--json` included):

1. services active + enabled (`uii-hub`, `uii-mqtt-shim`, `mosquitto` if configured);
   `systemctl --failed` empty.
2. listeners bound: 7300, 8400, broker reachable (TCP connect to each configured broker).
3. **heard vs configured**: subscribe to `tele/+/heartbeat` on each broker for 12 s; list
   pi_ids heard; flag configured keys never heard and heard pi_ids not configured
   (this is exactly the `nh4mod` vs `nh4mod-01` trap).
4. hub view: each configured unit OPERATIONAL, `last_seen_s_ago` < 3× heartbeat period,
   slot occupied, no active alerts unless expected.
5. clock: hub NTP synced; unit heartbeat skew < 300 s.
6. network: each broker address on a directly connected subnet (catches the eth0/eth1 and
   192.168 vs 10.10.11 mismatch before anyone sniffs frames).

Exit 0 PASS / 1 WARN / 2 FAIL. `install.sh` and `sync-hrsd-hub.sh` end by running it.

## Phase 5 — Tests for the shim path (M)

`tests/test_mqtt_shim.py`, using the bench harness in `tests/helpers.py`, a real
`mosquitto` on an ephemeral port (skip the module with a clear message if the binary is
absent; it is on the Mac and on the Pi) and `tools/fake_legacy_unit.py`:

- adopt via shim → OPERATIONAL, mode from heartbeat.
- take_control → prime: exactly one ACK, PROGRESS only on change, one RESULT; rejected →
  ACK accepted=false with the unit's reason.
- unit goes silent → BYE → REMOVED; unit returns → re-adopt without shim restart.
- broker killed and restarted → shim reconnects and re-subscribes; commands work after.
- broker frozen (SIGSTOP, no FIN) → shim notices within 1.5× keepalive (the half-open case).
- hub restarted → shim re-HELLOs, module OPERATIONAL again within 10 s.
- config with a wrong unit key → doctor reports "heard `nh4mod`, configured `nh4mod-01`".

## Phase 6 — CLI polish (S)

- `uii/cli.py` watch loop: print a progress line only when `(pct, message)` changes; add
  elapsed seconds; print the RESULT once. (Phase 1's dedupe removes the flood at source;
  this keeps the CLI honest against a chatty module.)
- Help text: make it obvious that `--json` is a global flag (`uii --json modules`, not
  `uii modules --json`); accept it in both positions.

## Phase 7 — Unit side, with Arba (needs the unit's login)

Decision to record: **a legacy unit keeps its own broker; the shim subscribes to it.** The
unit then works standalone with the WPF app and with the hub, and needs no config change
at adoption. Repointing `MQTT_HOST` to the hub is no longer the plan for the shim era; it
returns with pimod / the MCU front-end, which speak UII natively.

Remaining unit fixes once we have SSH access (ask Arba for the `pi` login or a key):
- default gateway `255.255.255.0` → either none (isolated LAN) or the hub's 192.168.1.20.
- time: run chrony on the hub serving eth0, point the unit at 192.168.1.20; until then the
  hub keeps stamping envelopes with its own clock (it already does; the unit's `ts` is
  only inside the payload).
- optionally move the unit to eth1 / 10.10.11.x so the instrument LAN is real; not
  required for anything above.

## Phase 8 — Field verification runbook (on the box, after sync)

1. `sudo reboot` → within 60 s: `systemctl --failed` empty, `uii doctor` PASS.
2. Power-cycle the unit → `uii modules` shows BYE/REMOVED, then OPERATIONAL again with no
   hands on the hub; journal shows one line each way.
3. `sudo systemctl restart uii-hub` → module back within 10 s.
4. `take_control` → `prime` → `bridge`: one ACK / changing PROGRESS / one RESULT each, and
   the unit's heartbeat `mode_reason` carries the hub's command id.
5. Record the run in the vault under `12-project-delivery/hrsd/` (HRSD-002 workstream).

## Order and size

| Phase | Size | Blocks on |
|---|---|---|
| 0 commit split | S | nothing |
| 1 shim robustness | M | 0 |
| 2 units + ordering | S | 0 |
| 3 site overlay + sync | M | 2 |
| 4 doctor | M | 1 (per-unit broker) |
| 5 tests | M | 1, 4 |
| 6 CLI polish | S | nothing |
| 7 unit side | S | Arba's credentials |
| 8 field verification | S | 1–6 synced |

Phases 1, 2 and 6 can go in one sitting; 3, 4 and 5 in a second; 8 is the sign-off.
