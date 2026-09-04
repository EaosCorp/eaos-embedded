"""Classical foam-coverage CV — the first vision observable.

No learned model, no training set: foam on an aeration/clarifier surface
scatters light, so it reads brighter and less saturated than the dark,
moderately-saturated water around it. Nuisance foam is bright and white;
biological (Nocardia/Microthrix) foam is a warmer tan/brown. We segment on
those two honest physical signals and report the fraction of the region of
interest that is foam, plus a coarse type and an image-quality score that
drives the permitted-use designation on the hub.

Pure numpy (the vision extension's only compute dependency). Deterministic:
the same frame and params always yield the same numbers, and the params
carry a version id so a derived value is reproducible from the stored frame
— the vision analog of "every concentration is recomputable from its cal."

    from uii_vision.foam_cv import detect, MODEL_ID, rect_roi
    out = detect(rgb_uint8, roi=rect_roi(rgb_uint8.shape, 0, 0.2, 1, 1))
    out["coverage_pct"], out["foam_type"], out["image_quality"]
"""
from __future__ import annotations

import numpy as np

MODEL_ID = "foam-cv/0.1.0"

# Tunable thresholds. Conservative defaults for a typical daytime basin view;
# a site overrides these in role_config and the version id changes with them.
DEFAULTS = {
    # white / nuisance foam: bright and washed-out
    "white_v_min": 0.62,
    "white_s_max": 0.28,
    # biological / brown foam: bright-ish, moderate saturation, warm (R>B)
    "brown_v_min": 0.40,
    "brown_s_min": 0.15,
    "brown_s_max": 0.60,
    "brown_warm_min": 0.06,      # (R-B) in [0,1]
    # image quality
    "bright_lo": 0.12,           # mean V below this = too dark
    "bright_hi": 0.92,           # mean V above this = washed out
    "glare_v_min": 0.98,         # blown-highlight pixel
    "glare_s_max": 0.04,
    "sharp_ref": 0.045,          # gradient energy that reads as "in focus"
    # a coverage below this reads as no foam (segmentation noise floor)
    "coverage_floor_pct": 0.5,
}


def _rgb_to_vsh(rgb: np.ndarray):
    """Return (V, S, warm) in [0,1]. warm = R-B (foam tint cue)."""
    x = rgb.astype(np.float32)
    if x.max() > 1.0:
        x = x / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    v = np.max(x, axis=-1)
    mn = np.min(x, axis=-1)
    s = np.where(v > 1e-6, (v - mn) / np.maximum(v, 1e-6), 0.0)
    warm = r - b
    return v, s, warm


def rect_roi(shape, x0=0.0, y0=0.0, x1=1.0, y1=1.0) -> np.ndarray:
    """Boolean HxW ROI from fractional corners (0..1). Default = whole frame."""
    h, w = shape[0], shape[1]
    mask = np.zeros((h, w), dtype=bool)
    xa, xb = int(round(x0 * w)), int(round(x1 * w))
    ya, yb = int(round(y0 * h)), int(round(y1 * h))
    mask[max(0, ya):min(h, yb), max(0, xa):min(w, xb)] = True
    return mask


def polygon_roi(shape, polygons) -> np.ndarray:
    """Boolean HxW ROI = union of polygons given as fractional vertices
    [[x, y], ...] (0..1). `polygons` may be a list of polygons or a dict
    name -> polygon. Even-odd ray casting, vectorised over the pixel grid, so
    concave outlines (a channel notched around a mixer platform) work. Pure
    numpy; a 1920x1080 frame with a dozen edges rasterises in ~50 ms."""
    h, w = shape[0], shape[1]
    mask = np.zeros((h, w), dtype=bool)
    polys = list(polygons.values()) if isinstance(polygons, dict) else list(polygons or [])
    if not polys:
        return mask
    ys = (np.arange(h, dtype=np.float64) + 0.5)[:, None]
    xs = (np.arange(w, dtype=np.float64) + 0.5)[None, :]
    for poly in polys:
        pts = [(float(x) * w, float(y) * h) for x, y in poly]
        if len(pts) < 3:
            continue
        inside = np.zeros((h, w), dtype=bool)
        n = len(pts)
        for i in range(n):
            (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % n]
            if y0 == y1:
                continue
            # rows whose centre lies between the edge's y extents (half-open)
            rows = ((ys >= min(y0, y1)) & (ys < max(y0, y1)))
            # x of the edge at each row's centre
            xi = x0 + (ys - y0) * (x1 - x0) / (y1 - y0)
            inside ^= rows & (xs < xi)
        mask |= inside
    return mask


def build_roi(shape, roi) -> np.ndarray | None:
    """Turn an ROI spec into a mask. Accepts None (whole frame), a rectangle
    as (x0, y0, x1, y1) or {"x0","y0","x1","y1"}, or {"polygons": {...}}."""
    if roi is None:
        return None
    if isinstance(roi, dict):
        if roi.get("polygons"):
            return polygon_roi(shape, roi["polygons"])
        if {"x0", "y0", "x1", "y1"} <= set(roi):
            return rect_roi(shape, roi["x0"], roi["y0"], roi["x1"], roi["y1"])
        return None
    return rect_roi(shape, *roi)


def region_masks(shape, roi) -> dict[str, np.ndarray]:
    """Per-region masks for a polygon ROI ({name: mask}); empty otherwise."""
    if isinstance(roi, dict) and isinstance(roi.get("polygons"), dict):
        return {k: polygon_roi(shape, [v]) for k, v in roi["polygons"].items()}
    return {}


def _sharpness(v: np.ndarray, roi: np.ndarray) -> float:
    """Mean gradient energy inside the ROI — a focus/blur proxy."""
    gx = np.abs(np.diff(v, axis=1))
    gy = np.abs(np.diff(v, axis=0))
    rx = roi[:, 1:] & roi[:, :-1]
    ry = roi[1:, :] & roi[:-1, :]
    e = 0.0
    n = 0
    if rx.any():
        e += float(gx[rx].sum()); n += int(rx.sum())
    if ry.any():
        e += float(gy[ry].sum()); n += int(ry.sum())
    return e / n if n else 0.0


def detect(rgb: np.ndarray, roi: np.ndarray | None = None, params: dict | None = None) -> dict:
    """Foam coverage over an ROI. Returns coverage %, type, confidence, and
    an image-quality score with its components. Never raises on a bad frame;
    a degenerate frame comes back as low quality / zero coverage."""
    p = {**DEFAULTS, **(params or {})}
    rgb = np.asarray(rgb)
    if rgb.ndim == 2:  # grayscale -> stack
        rgb = np.stack([rgb] * 3, axis=-1)
    h, w = rgb.shape[0], rgb.shape[1]
    if roi is None:
        roi = np.ones((h, w), dtype=bool)
    roi_n = int(roi.sum())
    if roi_n == 0:
        return _empty(p, reason="empty_roi")

    v, s, warm = _rgb_to_vsh(rgb)

    white = (v >= p["white_v_min"]) & (s <= p["white_s_max"])
    brown = ((v >= p["brown_v_min"]) & (s >= p["brown_s_min"]) &
             (s <= p["brown_s_max"]) & (warm >= p["brown_warm_min"]))
    foam = (white | brown) & roi
    wf = int((white & roi).sum())
    bf = int((brown & roi).sum())
    foam_n = int(foam.sum())
    coverage = 100.0 * foam_n / roi_n

    # ---- image quality ----
    mean_v = float(v[roi].mean())
    if mean_v <= p["bright_lo"]:
        bright_score = max(0.0, mean_v / p["bright_lo"])
    elif mean_v >= p["bright_hi"]:
        bright_score = max(0.0, (1.0 - mean_v) / (1.0 - p["bright_hi"]))
    else:
        bright_score = 1.0
    glare = float(((v >= p["glare_v_min"]) & (s <= p["glare_s_max"]) & roi).sum()) / roi_n
    glare_score = max(0.0, 1.0 - glare / 0.25)          # 25% blown = 0
    sharp = _sharpness(v, roi)
    sharp_score = min(1.0, sharp / p["sharp_ref"])
    quality = round(0.34 * bright_score + 0.33 * glare_score + 0.33 * sharp_score, 4)

    # ---- type + confidence ----
    if coverage < p["coverage_floor_pct"]:
        foam_type = "none"
    elif wf >= 2 * max(bf, 1):
        foam_type = "nuisance_white"
    elif bf >= 2 * max(wf, 1):
        foam_type = "biological_brown"
    else:
        foam_type = "uncertain"
    # separation: foam should be clearly brighter than the rest of the ROI
    non_foam = roi & ~foam
    sep = 0.0
    if foam_n and int(non_foam.sum()):
        sep = float(v[foam].mean() - v[non_foam].mean())
    sep_score = min(1.0, max(0.0, sep / 0.25))
    confidence = round(quality * (0.5 + 0.5 * sep_score), 4)

    return {
        "model_id": MODEL_ID,
        "coverage_pct": round(coverage, 2),
        "foam_type": foam_type,
        "confidence": confidence,
        "image_quality": quality,
        "quality_components": {
            "brightness": round(bright_score, 4),
            "glare": round(glare_score, 4),
            "sharpness": round(sharp_score, 4),
            "mean_v": round(mean_v, 4),
        },
        "foam_pixels": foam_n,
        "roi_pixels": roi_n,
        "white_pixels": wf,
        "brown_pixels": bf,
        "params": p,
    }


def _empty(p, reason=""):
    return {
        "model_id": MODEL_ID, "coverage_pct": 0.0, "foam_type": "none",
        "confidence": 0.0, "image_quality": 0.0,
        "quality_components": {"brightness": 0.0, "glare": 0.0,
                               "sharpness": 0.0, "mean_v": 0.0},
        "foam_pixels": 0, "roi_pixels": 0, "white_pixels": 0,
        "brown_pixels": 0, "params": p, "reason": reason,
    }


def permitted_use(cv: dict, min_quality: float = 0.45,
                  min_confidence: float = 0.4, control_ok: bool = False) -> tuple[str, list]:
    """Map a foam-CV result to the UII permitted-use designation.
    'none' if the frame can't be trusted; 'reporting' for a good read;
    'control' only if the site has qualified this view for closed-loop use."""
    flags = []
    if cv.get("reason") == "empty_roi":
        return "none", ["empty_roi"]
    if cv["image_quality"] < min_quality:
        flags.append("low_image_quality")
    if cv["confidence"] < min_confidence:
        flags.append("low_confidence")
    if flags:
        return "none", flags
    return ("control" if control_ok else "reporting"), []
