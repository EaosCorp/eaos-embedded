#!/usr/bin/env python3
"""Frame-store retention sweep (runs ON THE BOX, uii-retain.timer, daily).

Tiers by frame age (from the observation time in evidence.db, falling back to
file mtime):
  0..FULL_DAYS            keep everything (full-res)              default 30 d
  FULL_DAYS..THIN_DAYS    keep 1 frame per THIN_MIN minutes       default 90 d / 10 min
  > THIN_DAYS             keep nothing
plus, at every age, any frame referenced by a digest manifest
(/var/lib/eaos/digest/*/manifest.json) is kept. Safety valve: while free space
on the frames filesystem is below MIN_FREE_PCT, the oldest full-res day is
thinned early. evidence.db is never touched; each run appends a sweep event
via a small JSON line in /var/lib/eaos/digest/retention.log so deleted-blob
references remain explainable.

    python3 -m extensions.vision.tools.retain --dry     # report only
    python3 -m extensions.vision.tools.retain
Env: UII_RETAIN_FULL_DAYS, UII_RETAIN_THIN_DAYS, UII_RETAIN_THIN_MIN, UII_RETAIN_MIN_FREE_PCT.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone

DB = "/var/lib/eaos/uii-data/evidence.db"
FRAMES = "/var/lib/eaos/frames"
DIGEST = "/var/lib/eaos/digest"
LOG = os.path.join(DIGEST, "retention.log")
E = os.environ.get
FULL_DAYS = float(E("UII_RETAIN_FULL_DAYS", "30"))
THIN_DAYS = float(E("UII_RETAIN_THIN_DAYS", "90"))
THIN_MIN = float(E("UII_RETAIN_THIN_MIN", "10"))
MIN_FREE_PCT = float(E("UII_RETAIN_MIN_FREE_PCT", "15"))


def frame_times():
    """hash -> epoch seconds, from the frames observations (cheap: one column scan)."""
    t = {}
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        for tm, body in c.execute("select time, body from envelopes where kind='observation' and channel='frames'"):
            try:
                h = json.loads(body)["data"]["frame_hash"]
            except Exception:
                continue
            t[h] = datetime.fromisoformat(tm.replace("Z", "+00:00")).timestamp()
    return t


def digest_keep():
    keep = set()
    for man in glob.glob(os.path.join(DIGEST, "*", "manifest.json")):
        try:
            for it in json.load(open(man))["items"]:
                keep.add(it["frame_hash"])
        except Exception:
            pass
    return keep


def plan(now=None):
    now = now or time.time()
    times = frame_times(); keep = digest_keep()
    files = []
    for shard in os.listdir(FRAMES):
        d = os.path.join(FRAMES, shard)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".jpg"):
                continue
            h = fn[:-4]; p = os.path.join(d, fn)
            ts = times.get(h) or os.path.getmtime(p)
            files.append((ts, h, p, os.path.getsize(p)))
    files.sort()
    delete, kept_full, kept_thin, kept_digest = [], 0, 0, 0
    last_slot = None
    for ts, h, p, sz in files:
        age_d = (now - ts) / 86400.0
        if h in keep:
            kept_digest += 1; continue
        if age_d <= FULL_DAYS:
            kept_full += 1; continue
        if age_d <= THIN_DAYS:
            slot = int(ts // (THIN_MIN * 60))
            if slot != last_slot:
                last_slot = slot; kept_thin += 1; continue
        delete.append((p, sz, h, age_d))
    # safety valve: thin the oldest full-res days while free space is short
    st = shutil.disk_usage(FRAMES); free_pct = 100.0 * st.free / st.total
    valve = []
    if free_pct < MIN_FREE_PCT:
        projected = st.free + sum(s for _, s, _, _ in delete)
        kept_set = {h for _, h, _, _ in files} - {h for _, _, h, _ in delete}
        days = {}
        for ts, h, p, sz in files:
            if h in kept_set and h not in keep and (now - ts) / 86400.0 <= FULL_DAYS:
                days.setdefault(int(ts // 86400), []).append((ts, h, p, sz))
        for day in sorted(days):
            if projected >= MIN_FREE_PCT / 100.0 * st.total:
                break
            last = None
            for ts, h, p, sz in days[day]:
                slot = int(ts // (THIN_MIN * 60))
                if slot == last:
                    valve.append((p, sz, h, (now - ts) / 86400.0)); projected += sz
                last = slot
    return {"files": len(files), "kept_full": kept_full, "kept_thin": kept_thin, "kept_digest": kept_digest,
            "delete": delete, "valve": valve, "free_pct": round(free_pct, 1),
            "bytes_to_delete": sum(s for _, s, _, _ in delete + valve)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry", action="store_true"); a = ap.parse_args()
    pl = plan()
    print(f"{pl['files']} frames: keep full {pl['kept_full']}, thinned {pl['kept_thin']}, digest {pl['kept_digest']}; "
          f"delete {len(pl['delete'])} (+{len(pl['valve'])} valve) = {pl['bytes_to_delete'] / 1e9:.2f} GB; free {pl['free_pct']}%")
    if a.dry:
        return
    n = 0
    for p, _, _, _ in pl["delete"] + pl["valve"]:
        try:
            os.remove(p); n += 1
        except FileNotFoundError:
            pass
    os.makedirs(DIGEST, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(), "deleted": n, "bytes": pl["bytes_to_delete"],
                            "valve": len(pl["valve"]), "kept_full": pl["kept_full"], "kept_thin": pl["kept_thin"],
                            "kept_digest": pl["kept_digest"], "free_pct_before": pl["free_pct"],
                            "policy": {"full_days": FULL_DAYS, "thin_days": THIN_DAYS, "thin_min": THIN_MIN}}) + "\n")
    print(f"deleted {n}")


if __name__ == "__main__":
    main()
