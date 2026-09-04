# Legacy shim — mapping NH4MOD PROD4 onto UII

Source examined 2026-07-08: `serial_mqtt_gateway_nh4_nox_po4.py`
(A. Williamson, 2026-03) + the C# WPF UI (`MainWindow.xaml.cs`, MQTTnet client).
Source now lives at `~/Desktop/_cleanup-2026-07-15/zz-unclear/NH4MOD PROD4/`
(was `~/Desktop/NH4MOD PROD4/`).

> **2026-07-26: superseded by `uii_analyzer/pimod.py`.** Instead of a Stage-1 MQTT shim
> alongside the legacy gateway, the gateway's hardware logic (split-PLC
> bridge, ST9 timelines, ADC capture) was ported into a native UII module
> agent — see `DEPLOY.md` for the cutover. This doc remains the reference
> for what the legacy system was and how its surface maps onto UII.
>
> **2026-08-25: the Stage-1 shim exists after all** — `uii_analyzer/mqtt_shim.py`,
> built for the HRSD Nansemond POC so units still running the legacy gateway
> (unmodified, just repointed at the hub box's broker) appear on the hub as
> real modules. The mapping table below is its spec. pimod / the MCU
> front-end remains the endstate; the shim retires unit-by-unit.
>
> **Gateway builds** we hold, dated by receipt, live in
> `eaos-clients/hrsd/reference/arba-nh4mod-gateway/` (2026-03 multi-analyte; 2026-09 NH4 build
> with `prime2` and `results` as `{vin, absorbance, fit, nh4_mgL, error}`).

## What the current system actually is

```
C# WPF UI (Windows)  ── MQTT sub tele/{pi_id}/* · pub cmd/{pi_id}/action ──┐
                                                                           v
                          MQTT broker (host/IP typed into the UI)
                                                                           ^
Pi (gateway python) ───────────────────────────────────────────────────────┘
  Port A /dev/ttyUSB0 = PLC (controller)          9600 8N1 line-based
  Port B /dev/ttyUSB1 = pump controller           EZ-stepper ASCII "/1...R"
  ADS1115 on I2C      = detector (the Pi reads it directly)
  BRIDGE mode  : PLC drives — Pi forwards serial A<->B (plant authority)
  ENDPOINT mode: Pi drives — timeline tables send ST9:N strings (latched)
  Cal math     : absorbance = log10(i0/i1); conc = slope*A + intercept (on the Pi)
  Persistence  : calibration.json + JSONL append logs in /opt/.../data
```

Key discoveries that change the migration plan:

1. **There is no custom MCU firmware.** The "module MCU" today is an
   off-the-shelf stepper/pump controller speaking EZ-stepper ASCII. The method
   engine is (a) the PLC program in bridge mode, (b) Python timeline tables in
   endpoint mode. So Stage 5's "wrap the MCU" becomes "give the pump controller
   an MCU front-end" (the STM32H563 board *absorbs* the ST9 tables as its
   method engine and drives the pump controller over serial — or replaces it).
2. **Interpretation already happens above the actuator** (Pi computes
   absorbance + concentration). Hub-side derivation is not a change, it is a
   formalization. calibration.json becomes a calibration *envelope*.
3. **The C# UI is already just a client** (pure MQTT display/control). It can
   be pointed at the hub's broker or replaced by the /v1 API + SSE with no
   architecture change.
4. **BRIDGE vs ENDPOINT is the two-plane split in embryo**: bridge = PLC/plant
   authority, endpoint = hub authority. The UII command gateway + OT adapter
   replace the mode latch with per-command arbitration.

## Topic → envelope mapping (for the Stage-1 shim service)

| Legacy MQTT | UII envelope |
|---|---|
| `cmd/{pi}/action` {action, request_id} | `command` (command_id = request_id) |
| `tele/{pi}/action_status` state=started/progress | `ack` / `progress` |
| `tele/{pi}/action_status` state=completed/failed/rejected | `result` |
| `tele/{pi}/data` (adc_capture) | `observation` channel=detector_raw |
| `tele/{pi}/results` (mg/L) | `observation` channel=nh4 (derived) |
| `tele/{pi}/run_summary` | `result` outputs |
| `tele/{pi}/raw` (serial TX/RX) | `event` (serial trace) |
| `tele/{pi}/heartbeat` | `health` |
| calibration.json write | `calibration` |
| take_control / bridge | `config` (mode change, audited) |

Stage-1 shim = a small service subscribing to the legacy topics and writing
envelopes into the evidence store, so the existing gateway keeps working
untouched while the evidence log starts accumulating on day one.

## Command set (confirmed from source)

`take_control|endpoint`, `bridge`, `prime`, `prime2` (2026-09 build: ST9 35 then 88,
15 s), `calibrate` (params: std_conc, NOX also 5x/NO2 variants), `sample` — plus raw pass-through `cmd/{pi}/tx/port_a|b`
(that pass-through must NOT survive into UII except as a dev-mode tool).

Action queue depth today: 3. Rejections already exist ("Not in ENDPOINT
mode", "Action queue full", "Unknown action") — these become problem+json types.
