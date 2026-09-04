#!/usr/bin/env python3
"""Curate a small, diverse labelling set from an edge box's frame store.

Runs ON THE BOX (cellular uplink: never pull the frame store). Reads the
foam_coverage observations in evidence.db (time, frame_hash, coverage, image
quality), buckets them by local hour-of-day x coverage band, samples evenly
across buckets so dawn / noon / dusk / overcast and low / mid / high coverage
are all represented, and writes downscaled JPEGs plus a manifest to an export
dir. Typical output: 150 frames at 640 px ~= 6 MB, cheap to pull.

    python3 -m extensions.vision.tools.curate_frames --n 150 --width 640 \
        --out /var/lib/eaos/curated/2026-08-23

    # then from the laptop (small):
    ssh dcwa-001-root 'tar czf - -C /var/lib/eaos/curated 2026-08-23' > dcwa-001-curated.tgz

The manifest carries hash, time, hour, coverage (union + regions), quality
and the ROI polygons in force, so labels can be joined back to evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

HOUR_BANDS = [(5, 8, "dawn"), (8, 11, "morning"), (11, 15, "midday"),
              (15, 18, "afternoon"), (18, 21, "dusk"), (21, 29, "night")]
COV_BANDS = [(0, 10, "c00-10"), (10, 30, "c10-30"), (30, 60, "c30-60"), (60, 101, "c60+")]


def band(x, bands):
    for lo, hi, name in bands:
        if lo <= x < hi:
            return name
    return bands[-1][2]


def load_rows(db, tz_offset_h, min_quality):
    c = sqlite3.connect(db)
    q = ("select time, body from envelopes where kind='observation' "
         "and channel='foam_coverage' order by seq")
    rows = []
    for t, body in c.execute(q):
        d = json.loads(body)
        data = d.get("data") or {}
        h = data.get("frame_hash")
        if not h:
            continue
        iq = data.get("image_quality") or 0.0
        if iq < min_quality:
            continue
        ts = datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(timezone.utc)
        local_h = (ts.hour + tz_offset_h) % 24
        rows.append({"frame_hash": h, "time": t, "local_hour": local_h,
                     "coverage_pct": data.get("value"), "foam_type": data.get("foam_type"),
                     "image_quality": iq, "regions": data.get("regions"),
                     "roi": (d.get("context") or {}).get("roi")})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/var/lib/eaos/uii-data/evidence.db")
    ap.add_argument("--frames", default="/var/lib/eaos/frames")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--width", type=int, default=640)
    ap.add_argument("--quality", type=int, default=72, help="JPEG quality")
    ap.add_argument("--min-quality", type=float, default=0.3, help="skip frames below this image_quality")
    ap.add_argument("--tz-offset", type=int, default=-4, help="local hours from UTC (Blue Plains EDT = -4)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    from PIL import Image
    rows = load_rows(a.db, a.tz_offset, a.min_quality)
    by_hash = {}
    for r in rows:                      # dedupe: one observation per frame
        by_hash.setdefault(r["frame_hash"], r)
    rows = [r for r in by_hash.values()
            if os.path.exists(os.path.join(a.frames, r["frame_hash"][:2], r["frame_hash"] + ".jpg"))]
    buckets = defaultdict(list)
    for r in rows:
        r["hour_band"] = band(r["local_hour"], HOUR_BANDS)
        r["cov_band"] = band(r["coverage_pct"] or 0.0, COV_BANDS)
        buckets[(r["hour_band"], r["cov_band"])].append(r)
    print(f"{len(rows)} frames with observations on disk; {len(buckets)} populated buckets")
    for k in sorted(buckets):
        print(f"  {k[0]:>10} {k[1]:>7}: {len(buckets[k])}")

    rnd = random.Random(a.seed)
    picked, keys = [], sorted(buckets)
    for k in keys:
        rnd.shuffle(buckets[k])
    while len(picked) < a.n and any(buckets[k] for k in keys):   # round-robin across buckets
        for k in keys:
            if buckets[k] and len(picked) < a.n:
                picked.append(buckets[k].pop())
    picked.sort(key=lambda r: r["time"])
    print(f"picked {len(picked)}")
    if a.dry:
        return

    os.makedirs(a.out, exist_ok=True)
    total = 0
    for r in picked:
        src = os.path.join(a.frames, r["frame_hash"][:2], r["frame_hash"] + ".jpg")
        name = f"{r['time'].replace(':', '').replace('-', '')[:15]}_{r['frame_hash'][:12]}.jpg"
        im = Image.open(src).convert("RGB")
        w, h = im.size
        if w > a.width:
            im = im.resize((a.width, round(h * a.width / w)), Image.LANCZOS)
        dst = os.path.join(a.out, name)
        im.save(dst, quality=a.quality, optimize=True)
        r["file"] = name
        r["source_size"] = [w, h]
        total += os.path.getsize(dst)
    manifest = {"hub": os.uname().nodename, "created": datetime.now(timezone.utc).isoformat(),
                "n": len(picked), "width": a.width, "frames_dir": a.frames,
                "hour_bands": HOUR_BANDS, "coverage_bands": COV_BANDS, "items": picked}
    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    print(f"wrote {len(picked)} files, {total / 1e6:.1f} MB -> {a.out}")


if __name__ == "__main__":
    main()
