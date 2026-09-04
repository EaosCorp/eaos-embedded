# Roadmap — staged extensions and planned work

> The core (see README read order) is deliberately small: the module
> contract + the evidence discipline. Everything below is either **staged**
> (built, tested, in `extensions/`, one config flag away) or **planned**
> (designed, documented, not yet code). The rule for adding anything: it
> must enter through a declared hook, and the core must not learn its name.

## The extension hooks (what the core exposes)

| Hook | Where | Used by |
|---|---|---|
| `hub.southbound.interpreters[instrument_class]` | result → derived evidence | analyzer (today); vision, rotating (planned) |
| `hub.gateway.policy` — decide() → allow / reject / **defer**, on_defer(), command_state() | command gate | authority |
| `hub.store.subscribe()` | evidence fan-out | detections (any watcher) |
| `hub.api_get_routes` / `hub.api_post_routes` / `hub.api_auth` | /v1 surface | detections, authority, exports |
| `hub.module_row_enrichers` | /v1/modules columns | detections (NE107 status) |
| `hub.add_service(thread)` | background engines | scheduler, detections |

## Recently landed in core (from the original spec/playbook notes)

Exposed command surface: `/v1/modules/{id}/commands` + `uii commands` —
every command with param schema, risk, declared **preconditions**
("state:idle", "mode:ENDPOINT"), and typical duration; the gateway
validates params against the declared schema and pre-rejects failed
preconditions with `precondition-failed`, so clients and agents predict
rejections instead of discovering them (spec §5.2/§6.3). Cooperative
cancel (`/v1/commands/{id}/cancel`). `/.well-known/uii` discovery.
`/v1/modules/{id}/history` (the digital record) and the
`/v1/observations` query. And the playbook's Stage 1–2 exit demo in
software: **the hub can power-cycle mid-run** — modules buffer
ack/progress/result through hub loss, the hub recovers command context
from the store, and a restart is never mistaken for a module loss
(one result per command survives both directions; tested).

## Staged (on the `staged` branch in `extensions/`, tested, enable via `"extensions": [...]`)

### analyzer — the field chemical-analyzer profile
NOX three-channel interpretation (NO3 = NOX − NO2 with physical-validity
rules), the deployed gateway's ST9 method timelines ported verbatim, real
hardware (split-PLC serial bridge, ADS1115), and **pimod** — the module
agent that replaces the legacy MQTT gateway on the actual NH4MOD Pi
(`python3 -m uii_analyzer.pimod`; DEPLOY.md is its runbook; sim
mode runs the identical code anywhere). *Enable for: any real analyzer
deployment.*

### scheduler — cadence attached to roles
Take control if the role says so → never sample uncalibrated
(cal-required event, or auto_calibrate) → samples on interval; survives
module swaps because schedules key to roles, not serials. The swap-drill
exit demo lives in its tests. *Enable for: hands-off operation.*

### detections — the hub watches itself
Declarative rules over the evidence stream (threshold with hysteresis +
debounce, stale-data with role-supersede anti-stale-alarm, cal-overdue,
health flags, quality streaks), alerts emitted as causation-linked
evidence, audited acknowledge, ISA-18.2-style state suppression, NAMUR
NE107 rollup per module, and the health watchdog (quiet module →
DEGRADED). Design + research grounding: `detections.md`. *Enable for: any
unattended deployment.*

### authority — who may run what, arriving how
Effective permission = min(actor class, ingress path ceiling); agents run
routine alone, disruptive defers to a human approval (audited both ways),
hazardous always does; path ceilings are absolute. Locked mode binds
identity to bearer tokens so an agent cannot claim to be a person.
*Enable for: any hub agents or remote paths can reach — and before any
plant-connected deployment.*

### exports — the record, portable
Evidence bundles: manifest + evidence.jsonl (chain intact) + chain proof +
a README written for the next reader (human or AI agent). Honest
contiguity flag; contiguous slices re-verify offline. *Enable for:
support, regulatory packages, refurb history, agent context.*

## Planned (designed, not yet code — rough order)

1. **CBOR southbound framing** — same message shapes, MCU-friendly bytes
   (protocol.py isolates this).
2. **Challenge-response module identity** — secure element on the H563
   module rev; replaces allowlist trust at VERIFYING (spec §5.4).
3. **Northbound: the box serves, the platform pulls and streams** — the
   evidence log is the queue (`seq` = offset); a collector on the Kafka side
   (a tailnet node) walks `/v1/evidence?since=` (or holds `/v1/events` SSE)
   and produces envelopes to a facility topic; the facility record is a sink
   of the topic. The box never initiates and holds no cloud credentials.
   Box-side prerequisites are small: SSE catch-up pagination (today caps at
   500 on connect), a distinct read-only collector token. Design + phases:
   vault `06-technical/architecture/instrument-interface/uii-results-egress-plan.md`.
   MQTT/CloudEvents severity routing is a later variant of the same outbox;
   any consumer of the legacy `tele/#` topics lands here.
3b. **Faceplate command buttons** — the read-only faceplate is BUILT
   (staged: `extensions/faceplate/`, one route + one page at `/faceplate`,
   Eaos dark tokens, SSE-live). Next: the small CURATED button row
   (config-driven per role, e.g. prime/sample/ack) submitting through the
   one gate under its own actor identity (`user:panel@…`) and a `panel`
   ingress ceiling. Deliberately never the main interface: buttons render
   only from the role's allowlist, the ceiling is enforced server-side,
   and the parity rule (nothing exists only on the panel) makes it
   structurally a subset. Later: the panel as the physical-presence
   second factor for hazardous approvals.
3c. **Chart-ready observations** — windowed/downsampled query
   (`/v1/observations?bucket=15m&since=7d`) so agents and UIs pull
   render-ready series instead of raw envelopes.
4. **OT adapter** — manifest-compiled Modbus register map; result tags gated
   by the permitted-use designation; NE107 status word; PLC request-tag
   handshake (a new ingress path with its own ceiling).
5. **Westgard QC engine** — scheduled control standards judged by
   Levey-Jennings multirules; drift vs random error as detections
   (detections.md §5.3; the analyzer-class flagship).
6. **Vision instrument class** — foam-detection camera: frames as facts, a
   vision interpreter at the interpreter hook, scene-obstructed/
   camera-moved detections. The class layering means this is a module
   agent + one interpreter; adoption/evidence/commands/API come free.
7. **Rotating instrument class** — centrifuges, pumps, blowers: run-hours,
   vibration/current signatures, service-due detections.
8. **Alarm shelving / out-of-service / flood controls** (ISA suppression
   states) on the detections engine.
9. **Trend detections** — calibration-slope trajectory, detector i0
   (lamp) degradation; predictive reagent depletion.
10. **mTLS / OAuth2 client-credentials** replacing bearer tokens; per-user
    roles; physical-presence second factor for hazardous commands.
11. **Signed update/package flow** (P3), retention policy (the blobstore has
    `sweep()`; no policy drives it yet), TIME sync as code (today it is ops
    units on the box: hourly NTP resync + hourly camera `SetTime` push, see
    the DC Water edge-box notes in the vault).
12. **Agent surface next steps** — runbook skills on the hub; optional MCP
    wrapper only when a shell-less agent surface needs one
    (`agent-interface.md`).

## What stays out, permanently

Per-module web servers · HTML as contract · raw pin-level control
surfaces · cloud dependency for local function · a second telemetry path
outside the evidence log · the hub acting as a safety system (interlocks
live on module hardware and work with the cable unplugged).
