# Deploying UII on the real NH4MOD Pi

This replaces `serial_mqtt_gateway_nh4_nox_po4.py` (and removes the need for
the MQTT broker, the Windows PC, and the C# UI). **The wiring does not
change**: PLC on port A, pump controller on port B, ADS1115 on I2C — exactly
as today. On first start the module comes up in **BRIDGE mode**, so the PLC
keeps driving the device and the plant sees no difference; you switch to
hub authority deliberately, when you choose to.

## 0. Rehearse anywhere first (no hardware)

```bash
python3 demo.py                                  # the core seam, ~20 s
python3 demo_full.py                             # everything enabled, ~90 s
python3 -m unittest discover -t . -s tests       # all 56 tests, ~50 s
```

Both must pass on the machine you're deploying from before touching the Pi.
The simulator runs the *identical* agent code, so what you rehearsed is what
runs on the hardware.

## 1. Install on the Pi

```bash
git clone <this repo> uii && cd uii     # or copy the folder over
sudo ./deploy/install.sh --with-hw      # /opt/uii, /etc/uii, /var/lib/uii
```

Edit the two config files:

**Lock the hub before it leaves the bench.** The example config ships with
`"credentials": {}` = open bench mode (self-declared actors — fine
air-gapped, never plant-connected). To lock, add real tokens, one per
person and one per agent process:

```json
"credentials": {
  "<openssl rand -hex 24>": "user:yourname",
  "<openssl rand -hex 24>": "agent:eddy-om@site"
}
```

With credentials present, every API/CLI call needs `--token` (or
`$UII_TOKEN`), identity comes from the token (claimed actors are ignored),
and approving a risk-gated command requires a `user:*` token. Verify with
`uii --token <t> system` → `"auth": "token"`.

* `/etc/uii/pimod.env` — copy the values from however the legacy gateway is
  launched today (`ANALYTE`, `PORT_A`, `PORT_B`, `BAUD`, `ADC_*` are the same
  variable names). Set `UII_SERIAL` to something permanent for this physical
  unit — history follows it.
* `/etc/uii/hub.json` — set `slot-1`'s `analyte` to match, keep
  `"extensions"` including at least `"analyzer"` (the field profile; the
  example enables all five), and leave `auto_take_control` /
  `auto_calibrate` **false** for the first cutover.

## 2. Cutover (reversible in one command)

The serial ports are exclusive, so the legacy gateway and the UII agent
cannot run at the same time.

```bash
# stop the legacy gateway however it is launched today (script/tmux/service)
sudo systemctl enable --now uii-hub uii-pimod
```

Smoke checks, in order:

```bash
uii system                 # hub answers
uii modules                # your module: state OPERATIONAL, mode BRIDGE
uii watch --kind event     # PLC serial traffic appears as evidence live
```

With the module in BRIDGE, the PLC runs the analyzer exactly as before —
except every serial line in both directions is now in the evidence log.
Let it sit like this as long as you want.

**Rollback** at any point:

```bash
sudo systemctl stop uii-pimod uii-hub
# start the legacy gateway again — nothing else changed
```

## 3. Taking control (when ready)

```bash
uii cmd take_control --module nh4mod-01 --watch     # latches ENDPOINT
uii cmd prime        --module nh4mod-01 --watch
uii cmd calibrate    --module nh4mod-01 --param std_conc=5.0 --watch
uii cmd sample       --module nh4mod-01 --watch     # prints mg/L + lineage id
uii obs                                             # faceplate
```

Notes that differ from the legacy gateway, on purpose:

* **Concentrations are computed on the hub**, not the Pi's script — every
  value carries its calibration ID and raw detector references
  (`uii lineage <id>` shows the whole chain). `calibration.json` is gone;
  calibration is an evidence record.
* **ENDPOINT stays latched** (same as the field latch today). `bridge`
  reports failure while latched; restart `uii-pimod` to return to BRIDGE.
* One TCP connection from the module agent to the hub replaces all MQTT
  topics. Telemetry buffers through hub restarts (ring buffer + redial).

## 4. Scheduled operation

When you're ready for hands-off cadence, in `/etc/uii/hub.json` set the
role's `auto_take_control: true`, `auto_calibrate: true`, and the
`sample_interval_s` you want, then `sudo systemctl restart uii-hub`. From
that point a freshly plugged (or swapped) module goes: adopted → controlled
→ calibrated → sampling, with no keyboard. That's the swap-drill behavior —
`python3 demo.py` shows it end to end.

## 5. What to send back when something's off

```bash
uii evidence --limit 50                       # last 50 envelopes
uii evidence --kind event --module nh4mod-01  # serial traces, faults
sqlite3 /var/lib/uii/evidence.db .dump | gzip > evidence-$(date +%F).sql.gz
```

The evidence DB is the whole story — commands, acks, raw volts, states,
health, identity transitions — hash-chained, so nothing is missing and
nothing can have been edited. Attach it; the bug becomes reproducible on
the software bench against the simulator.

## 6. Gateway-only hub with legacy units (the HRSD shape)

When the hub box has no analyzer of its own and the units still run the legacy MQTT gateway:

```sh
sudo ./deploy/install.sh --site hrsd-nans        # or without --site; edit /etc/uii/mqtt-shim.json
sudo systemctl enable --now uii-hub uii-mqtt-shim
uii doctor                                       # services · brokers · heard vs configured · hub view
```

`deploy/sites/<site>/` carries what is specific to a box (network addresses, the shim config,
the broker choice) so a re-image reproduces it; `deploy/sync-hrsd-hub.sh` pushes this checkout,
re-runs the installer, restarts the services and ends with `uii doctor`. Field verification after
any change: reboot the hub (`systemctl --failed` empty, doctor PASS, module OPERATIONAL within
60 s), power-cycle the unit (BYE, then re-adopt with no hands on the hub), restart `uii-hub`
(module back within 10 s), then `take_control` → `prime` → `bridge` with one ACK, changing
PROGRESS and one RESULT each.
