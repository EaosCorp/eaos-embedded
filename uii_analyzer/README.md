# uii_analyzer — the chemical-analyzer piece

The NH4MOD retrofit as a UII module: `pimod.py` (the field agent that runs on the instrument's Pi and
dials in to the hub), `hw.py` (hardware), `interpret_full.py` (full result interpretation, calibration
completeness), `timelines.py`. Enable on the hub with `"extensions": ["analyzer"]`; run the agent
with `python3 -m uii_analyzer.pimod`. Deploy runbook: `docs/DEPLOY.md`. Tests: `tests/test_analyzer.py`.

Also here: `mqtt_shim.py` (+ `mqtt_min.py`, a stdlib QoS-0 MQTT client) — the Stage-1 legacy shim,
revived 2026-08-25 for the HRSD POC. It presents units still running the legacy MQTT gateway as
UII modules: broker + shim on the hub box, zero software changes on the unit. Mapping per
`tools/legacy-shim.md`; bench partner: `tools/fake_legacy_unit.py`. Retire unit-by-unit as
modules move to pimod / the MCU front-end.
