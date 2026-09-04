# uii_analyzer — the chemical-analyzer piece

The NH4MOD retrofit as a UII module: `pimod.py` (the field agent that runs on the instrument's Pi and
dials in to the hub), `hw.py` (hardware), `interpret_full.py` (full result interpretation, calibration
completeness), `timelines.py`. Enable on the hub with `"extensions": ["analyzer"]`; run the agent
with `python3 -m uii_analyzer.pimod`. Deploy runbook: `docs/DEPLOY.md`. Tests: `tests/test_analyzer.py`.

Also here: `mqtt_shim.py` (+ `mqtt_min.py`, a stdlib QoS-0 MQTT client) — the Stage-1 legacy shim,
revived 2026-08-25 for the HRSD POC. It presents units still running the legacy MQTT gateway as
UII modules with zero software changes on the unit. Mapping per `tools/legacy-shim.md`; bench
partner: `tools/fake_legacy_unit.py`; tests: `tests/test_mqtt_shim.py` (needs a `mosquitto`
binary). Retire unit-by-unit as modules move to pimod / the MCU front-end.

**Where the broker is.** A legacy unit runs its own mosquitto (the gateway's default
`mqtt_host` is `127.0.0.1`), so the shim subscribes to the *unit's* broker — set `broker` per
unit in `/etc/uii/mqtt-shim.json`, or once at the top level when every unit has been repointed
at the hub box's broker. **The unit key must equal the unit's PI_ID** (legacy default `nh4mod`);
it is the topic prefix `tele/<pi_id>/#`. `python3 -m uii_analyzer.mqtt_shim --check` validates
the file and says what it will do; `uii doctor` (`doctor.py`) listens on each broker and
reports the pi_ids it *heard* against the keys *configured*, plus services, network, hub view
and clocks.

**What the shim guarantees** (each with a test): a unit appears on its first heartbeat and
says BYE after `offline_after_s` of silence, then re-adopts hands-off when it returns; a broker
outage or a half-open socket (unit lost power) is detected within 1.5 × `keepalive_s` and
reconnected; a hub restart re-adopts without touching the shim; PROGRESS is forwarded once per
distinct step; a unit that never answers is NACKed after `ack_timeout_s`; the unit's heartbeat
clock is compared with the hub's and surfaces as `clock_skew_s` in health. One journal line per
command: type, id, outcome, duration. Timeline actions (`prime`, `prime2`, `calibrate`, `sample`)
declare `mode:ENDPOINT` as a precondition, so the hub answers 422 at once when the unit is in
BRIDGE; `prime2` is the 2026-09 gateway's 15 s short prime (older builds answer 'Unknown action').
