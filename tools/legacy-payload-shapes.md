# Legacy payload shapes — extracted from Arba's gateway source

> Every shape below is reconstructed field-for-field from
> `serial_mqtt_gateway_nh4_nox_po4.py` (A. Williamson, 2026-03), which
> builds each payload as an explicit dict. This is the complete data
> surface the legacy system produced. Companion: `legacy-shim.md` (the
> topic → envelope mapping). Useful for parity checks when building the
> faceplate and the northbound/cloud ingest.

All topics were QoS 0, no retain — fire-and-forget, nothing persisted
beyond JSONL files on the Pi (`results.jsonl`, `adc_captures.jsonl`).

## Inbound (commands to the Pi)

`cmd/{pi_id}/action`
```json
{"action": "take_control|endpoint|bridge|prime|prime2|calibrate|sample",
 "request_id": "…",
 "std_conc": 5.0}                      // calibrate only
```
`cmd/{pi_id}/tx/port_a` and `/tx/port_b` — raw serial pass-through: the
payload is the literal line to write (no JSON). Deliberately not carried
into UII.

## Outbound telemetry

`tele/{pi_id}/heartbeat` — every 5 s
```json
{"ts": "...", "pi_id": "nh4mod", "analyte": "NH4",
 "mode": "BRIDGE|ENDPOINT", "mode_reason": "...", "endpoint_latched": false,
 "mqtt": "host:port", "port_a": "/dev/ttyUSB0", "port_b": "/dev/ttyUSB1",
 "port_a_ok": true, "port_b_ok": true, "port_a_err": "", "port_b_err": "",
 "adc_enabled": true, "uptime_s": 12345,
 "action": {"running": false, "name": null, "request_id": null,
            "elapsed_s": 0, "total_s": 0, "progress": 0, "message": ""}}
```

`tele/{pi_id}/action_status` — command lifecycle
```json
{"ts": "...", "request_id": "...",
 "action": "prime|prime2|calibrate|sample|take_control|bridge",
 "state": "started|progress|completed|failed|rejected",
 "message": "Sent ST9:42 (OK)", "progress": 37,
 "extra": {"std_conc": 5.0}}           // optional params echo
```

`tele/{pi_id}/data` — detector captures
```json
{"ts": "...", "type": "adc_capture", "pi_id": "...", "request_id": "...",
 "action": "calibrate|sample", "name": "NH4_CAL_DIW_I0",
 "vadc": 2.4001, "vin": 2.4001, "ma": null,
 "signal": {"vadc": 2.4001, "vin": 2.4001, "ma": null},
 "note": "t=123s"}
```
(`vin = vadc × divider_ratio`; `ma` populated only in `current_4_20`
signal mode as `vin/shunt_ohms × 1000`.)

`tele/{pi_id}/results` — calibration (NH4/PO4 shape)
```json
{"ts": "...", "type": "calibration", "analyte": "NH4",
 "units": {"concentration": "mg/L"}, "request_id": "...",
 "std_conc_mgL": 5.0,
 "vin": {"NH4_CAL_DIW_I0": 2.4, "NH4_CAL_DIW_I1": 2.39,
         "NH4_CAL_STD_I0": 2.4, "NH4_CAL_STD_I1": 1.51},
 "absorbance": {"diw": 0.001, "std": 0.201, "log_base": 10},
 "fit": {"slope": 25.0, "intercept": 0.03}, "error": ""}
```
NOX calibration variant: `vin` has 10 captures (NOX/NO2 × DIW/STD ×
I0/I1 + NOX_CAL_STD_I0/I1_5X); `absorbance` has `nox_diw, nox_std,
nox_std_5x, no2_diw, no2_std`; `fit` has `nox_slope, nox_intercept,
nox_slope_5x, nox_intercept_5x, no2_slope, no2_intercept`.

`tele/{pi_id}/results` — sample (NH4 shape; PO4 identical w/ `po4_mgL`)
```json
{"ts": "...", "type": "sample", "analyte": "NH4",
 "units": {"concentration": "mg/L"}, "request_id": "...",
 "cal_ts": "...",
 "vin": {"NH4_SAMP_I0": 2.4, "NH4_SAMP_I1": 1.6},
 "absorbance": {"sample": 0.176, "log_base": 10},
 "fit": {"slope": 25.0, "intercept": 0.03},
 "nh4_mgL": 4.37, "error": ""}
```
NOX sample variant: `vin` 4 captures; `absorbance` `{nox, no2}`; `fit`
all six fields; values `nox_mgL, no2_mgL, no3_mgL` (NO3 = NOX − NO2, null
+ error string when physically invalid or negative).

`tele/{pi_id}/run_summary`
```json
{"ts": "...", "request_id": "...", "analyte": "NH4",
 "action": "calibrate", "state": "completed|failed", "error": "",
 "std_conc_mgL": 5.0}                  // calibrate only
```

`tele/{pi_id}/raw` — serial trace, both directions
```json
{"ts": "...", "pi_id": "...", "port": "port_a|port_b",
 "dir": "TX|RX|TX_ERR", "raw": "/2S12gO4P19200...R", "err": "..."}
```

`tele/{pi_id}/port_a` and `/port_b`
```json
{"ts": "...", "pi_id": "...", "port": "port_a", "line": "..."}
```

## On-disk

`/var/lib/nh4mod/calibration.json`
```json
{"ts": "...", "analyte": "NH4", "units": {"concentration": "mg/L"},
 "std_conc_mgL": 5.0, "absorbance": {...}, "fit": {...}}
```
(NOX variant stores the six-field fit.)

## The honest gap

The gateway **never parses inbound serial**: PLC lines (port A) and
EZ-stepper replies (port B) are forwarded/logged as opaque strings. So
Arba's code defines the shape of everything the Pi *published*, but not
the meaning of what the pump controller or PLC *say back* — that
knowledge is in the EZ-stepper command-set docs and the PLC ladder
program, i.e. with Arba. Worth capturing at the bench (the historic
`mosquitto_sub -t '#' -v` dump ask from the playbook covers the same
ground from live traffic).

## 2026-09 build differences (single-analyte NH4 `serial_mqtt_gateway.py`)

- `prime2`: a 15 s short prime (ST9 35 at t=1, new ST9 88 at t=5). Same status states.
- `results` on calibrate: `{ts, type:"calibration", units:{concentration:"mg/L"}, request_id,
  std_conc_mgL, vin:{NH4_CAL_DIW_I0,...}, absorbance:{diw,std,log_base}, fit:{slope,intercept},
  error}`; on sample: `{ts, type:"sample", units, request_id, cal_ts, vin:{NH4_SAMP_I0,I1},
  absorbance:{sample,log_base}, fit, nh4_mgL, error}`. The shim copies `nh4_mgL` (or the older
  `value_mgL`) into `value` and marks the observation bad when `error` is non-empty.
- `run_summary` carries `state` + `error` instead of `err`.
- heartbeat adds `uptime_s` and an `action` block `{running, name, request_id, elapsed_s,
  total_s, progress, message}`.
- Reference copies of both builds: `eaos-clients/hrsd/reference/arba-nh4mod-gateway/`.
