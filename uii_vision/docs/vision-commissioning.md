# Commissioning a vision camera (foam, and level/weir)

> How you turn a camera bolted over a basin into a measurement you can trust.
> A camera that is adopted and streaming is not yet measuring anything useful:
> it needs to be told *where* to look (the ROI), *what clean looks like* (the
> baseline), and *how its pixels map to the world* (thresholds for foam, a
> scale for level). This is that procedure, done once per viewpoint, redone
> only when the view changes. Companions: `vision-profile.md` (design),
> `uii_vision/` (the code), `detections.md` (alert rules).

---

## 0. The mental model

- You commission a **role**, not a camera. A role is a viewpoint ("aeration
  basin 3, north end"). The role owns the ROI, the baseline, the thresholds,
  the schedule. Swap the physical camera and the role restores onto the new
  one (then re-baseline, because the new camera never aims *exactly* the same).
- **Everything is fractions, never pixels.** ROIs and datums are stored as
  fractions of the frame (0.0–1.0 across width and height). A fractional ROI is
  resolution-independent: it means the same thing on the 4K main stream, the
  sub stream, and the 1920-wide downscaled frame we store, and it survives a
  firmware change that alters resolution. Never write pixel coordinates into
  config.
- **Two places config lives:**
  - **Role config in `hub.json`** — durable, survives restart, travels through
    the promotion rings. This is where a commissioned ROI/baseline/thresholds
    belong.
  - **Runtime commands** (`set_region`, `re_baseline`) — take effect
    immediately and are recorded as evidence, for live adjustment while you
    dial a view in. *Current limitation:* on restart the module re-seeds from
    role config, so once you are happy, copy the values you found into
    `hub.json`. (Roadmap: reload the latest ROI/baseline envelope on adoption so
    runtime changes persist on their own.)

---

## 1. Draw the outline (the ROI)

> **2026-08-23: ROIs are polygons now.** A viewpoint's ROI is a set of named
> polygons, one per basin / channel, as fractional vertices:
> `{"polygons": {"basin-west": [[x, y], ...], "basin-east": [...]}}`. The box
> reads it from `/etc/eaos/vision-roi.json` (`UII_ROI_FILE`); canonical copies
> live in `uii_vision/config/roi/<hostname>.json`. Trace the **water surface
> only**: railings, walkways and mixer platforms are bright and read as foam
> to the threshold detector. Every `foam_coverage` observation reports the
> union plus a `regions` breakdown, so one foaming basin is visible on its own.
> The rectangle form (`x0,y0,x1,y1`) still works for a quick start. To draw:
> open a frame in a click-to-trace tool, copy the normalised JSON, validate
> with `tests/test_vision.py::PolygonRoiTest`.



The ROI is the rectangle the CV measures inside. For foam it is the stretch of
**water surface** you care about, with the walls, walkways, handrails, sky, and
fixed reflections left *out* (those are what produce false coverage).

1. **Aim and focus** the camera at the surface, then lock the mount. Any later
   pan/tilt/zoom invalidates the ROI and forces a re-baseline. While aiming,
   pull a fresh view each time from a laptop on the tailnet — an on-demand
   grab then the frame:
   ```
   POST /v1/commands  {"module":"campod-01","type":"capture"}     # operator token
   GET  /v1/modules/campod-01/frame                                # viewer token
   ```
   (`/frame` is `no-store`, so a browser tab on it refreshes to the newest one.)
   Get the camera on UTC before you start: the on-image timestamp is what you
   will compare against evidence times later.
2. **Grab a reference frame** to draw on:
   ```
   GET /v1/modules/<id>/frame?full=1     # the stored frame, ~200 KB
   GET /v1/modules/<id>/frame            # compressed preview, ~40-80 KB (cellular)
   ```
   Note its pixel size (it is in the frame's metadata, `w`/`h`, and in the image
   itself — the stored frame is capped at 1920 wide).
3. **Pick the rectangle** over just the surface, and convert its pixel corners
   to fractions:
   ```
   x0 = left_px  / width      y0 = top_px    / height
   x1 = right_px / width      y1 = bottom_px / height
   ```
4. **Set it.** Durably, in the role:
   ```json
   "roles": {
     "slot-1": {"role": "aer-3-foam",
                "roi": {"x0": 0.10, "y0": 0.35, "x1": 0.90, "y1": 0.95}}
   }
   ```
   Or live, to try one:
   ```
   POST /v1/commands
   {"module":"campod-01","type":"set_region",
    "params":{"roi":{"x0":0.10,"y0":0.35,"x1":0.90,"y1":0.95}}}
   ```
   Setting a region marks the module *re-baseline required* until you do step 2
   of §2 — a new region needs a new reference.

> A visual "draw the box on the live frame" tool (the Eddy Eng surface) is the
> intended operator experience; underneath it POSTs this same fractional
> rectangle. Polygon ROIs, for irregular basins, are a later extension of
> `set_region` — the rectangle is the starting primitive.

---

## 2. Capture the baseline (clean surface)

The baseline is a frame of the **foam-free** surface. It is what
`camera-moved` and `scene-obstructed` compare against, so drift and a bumped
camera raise a maintenance flag instead of a bad number.

```
POST /v1/commands {"module":"campod-01","type":"re_baseline"}
```

Do it when the surface is genuinely clean, and redo it after any legitimate
change (season, water level regime, a cleaned lens). The baseline is recorded
as an evidence reference record and clears the re-baseline-required state.

---

## 3. Tune the foam thresholds to the site

The classical detector keys on two physical facts: foam is **brighter** and
**less saturated** than the dark water, and biological (Nocardia/Microthrix)
foam is a **warmer tan** than white nuisance foam. Different plants, lighting,
and foam types need the thresholds nudged. They live in `foam_params` in the
role config:

```json
"slot-1": {"role":"aer-3-foam", "roi":{...},
           "foam_params": {"white_v_min":0.62, "white_s_max":0.28,
                           "brown_v_min":0.40, "brown_s_min":0.15,
                           "brown_s_max":0.60, "brown_warm_min":0.06,
                           "coverage_floor_pct":0.5}}
```

| knob | raises coverage when you… | use it when |
|---|---|---|
| `white_v_min` | lower it | bright foam is being missed (dim plant / dawn) |
| `white_s_max` | raise it | tinted white foam is being missed |
| `brown_v_min` | lower it | brown biological foam reads too dark to catch |
| `brown_warm_min` | lower it | brown foam is not warm enough to trip the warm test |
| `coverage_floor_pct` | lower it | small real foam patches are being zeroed as noise |
| `glare_s_max` / `glare_v_min` | (quality) | sun glint is being counted as foam |

**Method.** Grab a few frames spanning clean → heavy foam. For each, read the
operator's eyeball coverage and the CV's `foam_coverage`. Nudge one knob at a
time until they agree across the range. Keep the frames — under the sim rule
(`UII_SIM`, recorded frames) you can replay the exact tuning later.

**Type check.** Confirm `foam_type` matches what the operator sees (white
nuisance vs brown biological); the two drive different responses, so a
mislabel is worth a `brown_*` nudge.

---

## 4. Detections and the action level

Coverage is just a number until a rule says what is too much. Put the site's
foam philosophy in `hub.json` `detections` (mechanics in `detections.md`):

```jsonc
{"id":"aer3-foam-high","type":"threshold","channel":"foam_coverage",
 "raise_above":30,"clear_below":20,"debounce_s":120,"severity":"alert",
 "message":"foam over 30% of aeration basin 3"}
{"id":"aer3-scene-obstructed","type":"threshold","channel":"image_quality",
 "raise_below":0.4,"debounce_s":600,"severity":"warning",
 "ne107":"maintenance_required","message":"lens obstructed/fouled"}
{"id":"aer3-camera-moved","type":"scene_drift","raise_above":0.6,
 "severity":"alert","ne107":"check_function","message":"view moved; re-baseline"}
```

Set `raise_above` to the coverage where an operator would actually act, not a
round number.

---

## 5. Permitted use, and validation

Every foam number carries a permitted-use tag set from image quality. Keep the
view in **reporting** (`"control_ok": false`, the default) until you have
proven it:

- Over a week or two, compare `foam_coverage` against operator observations and
  the frames themselves (`/v1/modules/<id>/frame` at the time of an alert).
- Fix disagreements with §3 nudges; confirm bad frames (dark, glared,
  obstructed) already read `none` on their own.
- Only once a site has *qualified this specific view* for closed-loop use do you
  set `"control_ok": true` — and even then a control consumer must honor the
  per-frame permitted-use, so a fouled frame still cannot move anything.

That is the whole foam commissioning: **aim → ROI → baseline → tune → detect →
validate.**

---

## 6. Level / weir (TEMPLATE — the same shape, plus a scale)

`level_cv.py` is a scaffold, not a validated detector. Do not put a level
reading in front of an operator yet. When it is finished, commissioning is the
foam procedure with **one extra step, calibration**, because level is a
physical quantity where foam is a fraction.

1. **Aim** at the staff gauge, weir, or stilling well; lock the mount.
2. **Draw the ROI** as a *tall, narrow* box over the full vertical range the
   water line travels, including the gauge markings or weir crest. Fractions,
   exactly as §1.
3. **Calibrate the pixel scale** (the step foam does not need). The detector
   reports `surface_row_frac` (0 at the top of the ROI, 1 at the bottom). Record
   two known water levels from the staff gauge and the `surface_row_frac` the CV
   reports at each, then:
   ```
   units_per_frac = (level_hi - level_lo) / (frac_lo - frac_hi)
   ```
   (higher water = smaller `surface_row_frac`). Put it in the role:
   ```json
   "slot-1": {"role":"chan-1-level",
              "observables":["level"],
              "level":{"units_per_frac":1.8, "unit":"m"}}
   ```
   With a scale set, the detector reports a real `value` and marks itself
   `calibrated`; without one it stays a fraction and permitted-use is `none`.
4. **Weir datum (weir variant).** Survey the crest's position in the ROI and set
   `weir_crest_frac`; submergence = `level_frac − (1 − weir_crest_frac)`.
5. **Detections.** `level-high` / `level-low` thresholds on the `level` channel;
   `weir-overtopping` on submergence.
6. **Validate against the staff gauge** before trusting a single reading, then
   the same permitted-use discipline as foam.

The reason level reuses so much of foam is the point of the whole profile:
adding an observable is a new `detect()` plus a scale and some thresholds, not a
new system. See `level_cv.py`'s footer for the code wiring checklist.

---

## Quick reference

| thing | where | shape |
|---|---|---|
| ROI (durable) | `hub.json` role `roi` | `{"x0","y0","x1","y1"}` fractions |
| ROI (live) | `set_region` command | same |
| baseline | `re_baseline` command | (captures current frame) |
| foam thresholds | role `foam_params` | see §3 table |
| level scale | role `level.units_per_frac` | number + `unit` |
| weir datum | role `level.weir_crest_frac` | fraction |
| action levels | `hub.json` `detections` | see §4 / `detections.md` |
| qualify for control | role `control_ok` | `true`/`false` (default false) |
| fetch a frame | `GET /v1/modules/<id>/frame` | `?w=`, `?q=`, `?full=1`; `no-store`; `X-Frame-Hash`/`X-Frame-Time`; `HEAD` ok |
| frame by hash | `GET /v1/frames/<sha256>` | same params; `immutable` |
