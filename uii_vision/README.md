# uii_vision — the vision piece

The `vision` instrument-class profile of UII. Two interfaces:

- **campod** — the module agent (runs on the edge box next to the camera).
- **the hub interpreter** — classical-CV foam coverage (registered by `setup(hub)`).

Everything else (adoption, evidence, the command gate, scheduling, the API,
authority) is the universal core, unchanged. Design: `docs/vision-profile.md`.

## Two data shapes

- **Telemetry** — campod streams a frame every `cadence_s` as a `frames`
  observation; `FoamService` turns the stream into `foam_coverage`. No command
  in the loop.
- **Grab on demand** — a `capture` command grabs one frame now and returns its
  hash; the interpreter derives `foam_coverage` tied to the command's lineage.

Both produce the same `foam_coverage` channel, each frame carrying a
permitted-use designation (`reporting` / `none` / `control`) from image quality.

## Fetch images (compressed for cellular)

Frames are stored as JPEG, downscaled to 1920 wide (coverage is
scale-invariant). The API serves them **small by default** so a transfer over
cellular is cheap:

```
GET /v1/modules/<id>/frame           # latest frame, ~40-80 KB (1280 wide, q70)
GET /v1/frames/<sha256>              # a specific frame by hash
    ?w=960 ?q=60                     # smaller: ~15-40 KB
    ?full=1                          # the stored frame (~200 KB), opt-in only
HEAD <either>                        # same status + headers, no body
```

Both routes answer with `X-Frame-Hash` (sha256 of the stored frame) and, on
the per-module route, `X-Frame-Time` (the frame envelope's time), so a caller
can tell *which* frame it got without decoding it. Caching is deliberate:
`/v1/frames/<sha256>` is content-addressed and served `immutable`;
`/v1/modules/<id>/frame` is `no-store`, so a browser tab or `<img>` on that URL
refreshes to the newest frame instead of pinning the first one it saw.
`HEAD` works on every GET route (a probe on `/frame` is a cheap liveness
check) except the SSE stream `/v1/events` (405).

The CV always runs on the local full-quality frame on the box (no transfer);
only these fetches cross the wire, and only as small as you ask.

## Remote access (tailnet + token)

`edge.py` keeps module traffic on loopback (`UII_SB_BIND=127.0.0.1`) and faces
the API at `UII_API_BIND=0.0.0.0`, reachable over Tailscale (ACL-gated,
WireGuard-encrypted), not the internet. If a tokens file is present
(`UII_API_TOKENS`, default `/etc/eaos/uii-api-tokens.json`) the whole API
requires a bearer token (`auth.py`):

- **operator** token → GET + POST commands.
- **viewer** token → GET only (frames/observations); a POST is 403.

```bash
curl -H "Authorization: Bearer <viewer-token>" \
  http://<box-tailscale-ip>:8400/v1/modules/campod-01/frame -o frame.jpg
```

No tokens file => open mode (localhost dev / tests). Set
`UII_API_BIND=127.0.0.1` to keep the API local-only. For a browser or a
dashboard `<img>` the token may ride in the query string
(`/v1/modules/campod-01/frame?token=<viewer>`); fine on the tailnet, but URLs
get logged, so use the viewer token there, never the operator one.

## Deploying to an edge box

`deploy/sync-dcwa-edge.sh [host ...]` rsyncs this checkout onto the DC Water
edge boxes in their installed layout (`/opt/eaos/uii-analyzer`, vision under
`extensions/vision/`), keeps replaced files in a dated `.bak-` dir, byte-compiles,
restarts `uii-vision.service`, and probes `/.well-known/uii`. `DRY=1` previews.
It never touches `/etc/eaos` (creds, tokens) or `/var/lib/eaos` (frames,
evidence). Site specifics — camera DHCP reservations, cadence drop-in, box and
camera time sync — are ops units on the boxes, documented in the vault
(`01-company/credentials/dc-water/dcwa-blue-plains-edge-computers.md`).

## Commissioning (aim it at a real basin)

Adopted and streaming is not yet measuring. To turn a view into a trusted foam
(or level) number — draw the ROI, capture a baseline, tune thresholds,
validate — follow **`docs/vision-commissioning.md`**.

## Run it on the bench (sim, ~15 s)

```bash
python3 -m uii_vision.demo      # hub + campod in sim; prints live foam %
```

Or wire it by hand. Hub config (`hub.json`):

```json
{
  "hub_id": "hub-vision-001",
  "allowed_types": ["vision-cam"],
  "extensions": ["vision"],
  "data_dir": "./data",
  "roles": {
    "slot-1": {"role": "aeration-foam-cam", "cadence_s": 10, "control_ok": false,
               "roi": {"x0": 0.0, "y0": 0.15, "x1": 1.0, "y1": 1.0}}
  }
}
```

```bash
# terminal 1 — the hub
UII_CONFIG=hub.json python3 -m uii.hub.main
# terminal 2 — the camera agent, sim
UII_SIM=1 UII_SIM_COVERAGE=35 python3 -m uii_vision.campod
# grab on demand
uii capture campod-01        # or: POST /v1/commands {"module":"campod-01","type":"capture"}
```

## Run it against a real Reolink (the Blue Plains edge boxes)

campod reads the on-device credential already at `/etc/eaos/camera-credentials.env`:

```bash
set -a; . /etc/eaos/camera-credentials.env; set +a
UII_FRAMES_DIR=/var/lib/eaos/frames python3 -m uii_vision.campod
```

One hub per Jetson (co-located with campod); they share `UII_FRAMES_DIR`, so the
module writes a frame and the hub interpreter reads it back by hash.

## Commands (manifest)

| command | risk | note |
|---|---|---|
| `capture` | routine | grab one frame now; returns its hash |
| `set_cadence` | routine | change the telemetry interval |
| `set_exposure` | routine | imaging only |
| `re_baseline` | disruptive | new reference scene; clears re-baseline-required |
| `set_region` | disruptive | new ROI; forces re-baseline |
| `ptz` | disruptive | moves the view; forces re-baseline |

## Env

```
UII_HUB 127.0.0.1:7300   UII_MODULE_ID campod-01   UII_SLOT slot-1
UII_TYPE vision-cam      UII_SERIAL SN-...          UII_CADENCE_S 10
UII_FRAMES_DIR ./data/frames                        UII_SPEED 1
UII_JPEG_QUALITY 85   UII_FRAME_MAX_W 1920   (stored-frame codec; cellular)
UII_BIND 127.0.0.1    (edge.py binds localhost by default)
UII_SIM=1  UII_SIM_COVERAGE 25  UII_SIM_FOAM nuisance_white|biological_brown  UII_SIM_QUALITY 1.0
(real)  CAMERA_HOST / CAMERA_USER / CAMERA_PASSWORD  (from /etc/eaos/camera-credentials.env)
```

## Dependencies

The core is stdlib-only; this extension adds **numpy** (CV) and **Pillow**
(PNG encode/decode). `pip install numpy pillow`.

## Next observable: level/weir (template started)

`level_cv.py` is the **template** for the second observable — a working but
uncalibrated `detect()` (edge-based waterline; `level` and `weir` siblings)
that shows adding an observable is a known move: a `detect()` over a frame +
ROI, channels in the manifest, and a role_config flag. Adoption, evidence,
alerts, and the agent surface do not change. The three TODOs (pixel scale,
weir-crest datum, threshold tuning) plus the wiring checklist are in that file's
footer. It is **not wired into a live role** — finish and validate it first.
