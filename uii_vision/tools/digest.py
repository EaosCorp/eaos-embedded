#!/usr/bin/env python3
"""Daily frame digest: the few frames worth sending over cellular.

Runs ON THE BOX (timer: uii-digest.timer). For one local day it selects
  * keyframes  — the first good frame of every KEY_EVERY_H hours (full frame,
                 downscaled to KEY_W px, JPEG KEY_Q)          ~20 KB each
  * transitions — every frame where the learned model's bucket changed in
                 any region vs the previous frame, or any region's confidence
                 fell below LOW_CONF (saved as polygon-masked region CROPS at
                 CROP_W px, the model's own input; ~6 KB per region)
capped at MAX_FRAMES (transitions first). Nothing is deleted; the digest is a
copy under /var/lib/eaos/digest/<YYYY-MM-DD>/ with a manifest that carries the
model output for every item, so the laptop can auto-label and retrain from it.

    python3 -m extensions.vision.tools.digest            # yesterday (local)
    python3 -m extensions.vision.tools.digest --day 2026-08-22 --dry

Env knobs (set in the unit): UII_DIGEST_MAX (50), UII_DIGEST_KEY_EVERY_H (1),
UII_DIGEST_KEY_W (480), UII_DIGEST_KEY_Q (50), UII_DIGEST_CROP_W (256),
UII_DIGEST_CROP_Q (60), UII_DIGEST_LOW_CONF (0.6), UII_TZ_OFFSET_H (-4).
Budget at the defaults: ~0.9 MB/day. KEY_EVERY_H=2 -> ~0.6 MB/day.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np

DB = "/var/lib/eaos/uii-data/evidence.db"
FRAMES = "/var/lib/eaos/frames"
OUT = "/var/lib/eaos/digest"
ROI_FILE = "/etc/eaos/vision-roi.json"

E = os.environ.get
MAX_FRAMES = int(E("UII_DIGEST_MAX", "50"))
KEY_EVERY_H = int(E("UII_DIGEST_KEY_EVERY_H", "1"))
KEY_W, KEY_Q = int(E("UII_DIGEST_KEY_W", "480")), int(E("UII_DIGEST_KEY_Q", "50"))
CROP_W, CROP_Q = int(E("UII_DIGEST_CROP_W", "256")), int(E("UII_DIGEST_CROP_Q", "60"))
LOW_CONF = float(E("UII_DIGEST_LOW_CONF", "0.6"))
TZ_OFFSET_H = int(E("UII_TZ_OFFSET_H", "-4"))          # Blue Plains, EDT
MIN_QUALITY = float(E("UII_DIGEST_MIN_QUALITY", "0.3"))


def frame_path(h):
    return os.path.join(FRAMES, h[:2], h + ".jpg")


def load_day(day):
    """foam_region + foam_coverage observations for the local day, by frame."""
    tz = timezone(timedelta(hours=TZ_OFFSET_H))
    start = datetime.fromisoformat(day).replace(tzinfo=tz)
    end = start + timedelta(days=1)
    s, e = start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"), end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    c = sqlite3.connect(DB)
    rows = {}
    for t, ch, body in c.execute("select time, channel, body from envelopes where kind='observation' "
                                 "and channel in ('foam_region','foam_coverage') and time>=? and time<? order by seq", (s, e)):
        d = json.loads(body); data = d.get("data") or {}
        h = data.get("frame_hash")
        if not h:
            continue
        r = rows.setdefault(h, {"frame_hash": h, "time": t, "local_hour": (datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz)).hour})
        if ch == "foam_region":
            r["model_id"] = data.get("model_id"); r["regions"] = data.get("regions") or {}
        else:
            r["classical_pct"] = data.get("value"); r["image_quality"] = data.get("image_quality")
            r["quality_flags"] = (d.get("quality") or {}).get("flags", [])
    out = [r for r in rows.values() if os.path.exists(frame_path(r["frame_hash"]))]
    out.sort(key=lambda r: r["time"])
    return out


def select(rows):
    keys, trans, prev = [], [], None
    seen_slot = set()
    for r in rows:
        good = (r.get("image_quality") or 0) >= MIN_QUALITY
        slot = r["local_hour"] // KEY_EVERY_H
        if good and slot not in seen_slot:
            seen_slot.add(slot); keys.append(r); r["why"] = "keyframe"
        regs = r.get("regions") or {}
        if regs:
            reasons = []
            if prev:
                for n, v in regs.items():
                    pb = (prev.get(n) or {}).get("bucket")
                    if pb is not None and v["bucket"] != pb:
                        reasons.append(f"{n}:{pb}->{v['bucket']}")
            low = [n for n, v in regs.items() if v.get("confidence", 1) < LOW_CONF]
            if low:
                reasons.append("lowconf:" + ",".join(low))
            if reasons and good:
                if r.get("why") != "keyframe":
                    r["why"] = "transition"
                r["reasons"] = reasons; trans.append(r)
            prev = regs
    picked, ids = [], set()
    for r in trans + keys:                                     # transitions first, then keyframes
        if r["frame_hash"] in ids:
            continue
        if len(picked) >= MAX_FRAMES:
            break
        picked.append(r); ids.add(r["frame_hash"])
    picked.sort(key=lambda r: r["time"])
    return picked, len(trans), len(keys)


def region_crop(img, poly, size):
    from PIL import Image, ImageDraw
    W, H = img.size
    pts = [(x * W, y * H) for x, y in poly]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pw, ph = (max(xs) - min(xs)) * 0.04, (max(ys) - min(ys)) * 0.04
    box = (max(0, int(min(xs) - pw)), max(0, int(min(ys) - ph)), min(W, int(max(xs) + pw)), min(H, int(max(ys) + ph)))
    mask = Image.new("L", (W, H), 0); ImageDraw.Draw(mask).polygon(pts, fill=255)
    grey = Image.new("RGB", (W, H), (114, 114, 114))
    return Image.composite(img, grey, mask).crop(box).resize((size, size), Image.BILINEAR)


def main():
    ap = argparse.ArgumentParser()
    tz = timezone(timedelta(hours=TZ_OFFSET_H))
    ap.add_argument("--day", default=(datetime.now(tz) - timedelta(days=1)).strftime("%Y-%m-%d"))
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    from PIL import Image
    rois = json.load(open(ROI_FILE))["polygons"] if os.path.exists(ROI_FILE) else {}
    rows = load_day(a.day)
    picked, nt, nk = select(rows)
    print(f"{a.day}: {len(rows)} frames with observations; {nt} transition/low-conf, {nk} keyframes -> {len(picked)} selected (cap {MAX_FRAMES})")
    if a.dry:
        for r in picked:
            print("  ", r["time"], r.get("why"), r.get("reasons", ""), {n: v["bucket"] for n, v in (r.get("regions") or {}).items()})
        return
    out = os.path.join(OUT, a.day); os.makedirs(os.path.join(out, "crops"), exist_ok=True)
    total = 0; items = []
    for r in picked:
        img = Image.open(frame_path(r["frame_hash"])).convert("RGB")
        it = {k: r.get(k) for k in ("frame_hash", "time", "local_hour", "why", "reasons", "model_id", "regions",
                                    "classical_pct", "image_quality", "quality_flags")}
        stamp = r["time"].replace(":", "").replace("-", "")[:15]
        if r.get("why") == "keyframe":
            w, h = img.size; k = img.resize((KEY_W, round(h * KEY_W / w)), Image.LANCZOS)
            name = f"{stamp}_{r['frame_hash'][:12]}.jpg"; k.save(os.path.join(out, name), quality=KEY_Q, optimize=True)
            it["file"] = name; total += os.path.getsize(os.path.join(out, name))
        it["crops"] = {}
        for n, poly in rois.items():
            name = f"crops/{stamp}_{r['frame_hash'][:12]}_{n}.jpg"
            region_crop(img, poly, CROP_W).save(os.path.join(out, name), quality=CROP_Q, optimize=True)
            it["crops"][n] = name; total += os.path.getsize(os.path.join(out, name))
        items.append(it)
    man = {"hub": os.uname().nodename, "day": a.day, "tz_offset_h": TZ_OFFSET_H, "created": datetime.now(timezone.utc).isoformat(),
           "roi_polygons": rois, "crop": {"w": CROP_W, "q": CROP_Q, "mask": "polygon, grey 114 outside, pad 4%"},
           "keyframe": {"w": KEY_W, "q": KEY_Q, "every_h": KEY_EVERY_H}, "cap": MAX_FRAMES,
           "n_frames_day": len(rows), "n_transitions": nt, "n_keyframes": nk, "n_selected": len(picked), "bytes": total, "items": items}
    json.dump(man, open(os.path.join(out, "manifest.json"), "w"))
    print(f"wrote {len(items)} items, {total / 1e6:.2f} MB -> {out}")


if __name__ == "__main__":
    main()
