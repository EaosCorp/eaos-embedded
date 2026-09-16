#!/usr/bin/env python3
"""Push each camera's newest frame and its modules' readings from a UII hub to a work plane.

    publish_frames.py --plane https://<plane-host> --cameras cameras.yaml [--once] [--dry-run]

WHY THIS EXISTS. The boxes sit on the plant's tailnet, on cellular. A cloud work plane has no
route to a 100.x address and must not be given one: putting a cloud workload inside the plant's
private network is the wrong direction of trust, it multiplies across every plant, and a pull
multiplies cellular traffic by the number of readers (every case that asks for a frame, every
tile refresh). So the flow is outward and once per cadence: this reads each declared box over the
tailnet, and POSTs one frame plus its readings to the plane's mailbox, which serves every reader.

WHERE IT RUNS. Anywhere on the tailnet that can also reach the plane over HTTPS. This is the
reference implementation: it belongs in the hub itself as an extension (the hub already loads
extensions by config and already has a scheduler), and this file is what that extension does.
Until then, run it under systemd or cron on any box on the tailnet.

WHAT IT READS. `cameras.yaml` from the plant folder — the same file the plane reads, so there is
one declaration of what exists and at what cadence, not two. Cameras with `source: published` are
skipped: nothing is pushing them here.

CREDENTIALS, both from the environment, never on a command line:
    EAOS_UII_VIEWER_TOKEN      the box's viewer token (GET only)
    EAOS_FRAMES_INGEST_TOKEN   the plane's INGEST token -- it opens the mailbox and nothing else,
                               which is the one to put on a box. `EAOS_WORK_ACCESS_TOKEN` is taken
                               too, for an operator pushing by hand; never provision a box with it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

TIMEOUT = 30.0


def _log(msg: str) -> None:
    print(f"[publish-frames] {msg}", flush=True)


def _span_s(v, default: float = 900.0) -> float:
    """`15m`, `90s`, `1h` or a number of seconds. The plane reads this file the same way."""
    import re
    if v is None:
        return default
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else default
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smh]?)\s*", str(v))
    if not m:
        return default
    s = float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    return s if s > 0 else default


def _get(url: str, token: str, timeout: float = TIMEOUT) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def read_box(cam: dict, token: str) -> dict:
    """One camera's newest frame and its module's readings, from its box."""
    host, cid = cam.get("host"), str(cam["id"])
    mod = cam.get("module") or cid
    out: dict = {"camera": cid, "readings": {}}
    body, hdr = _get(f"http://{host}:8400/v1/modules/{mod}/frame?w=1280&q=70", token)
    out["image"] = body
    out["captured_at"] = hdr.get("x-frame-time")
    out["frame_hash"] = hdr.get("x-frame-hash")
    try:
        raw, _ = _get(f"http://{host}:8400/v1/modules", token, timeout=15)
        for m in (json.loads(raw).get("items") or []):
            if m.get("id") == mod:
                out["readings"] = {k: v for k, v in m.items()
                                   if k in ("foam_coverage_pct", "foam_type", "foam_permitted_use", "image_quality",
                                            "dose_class", "dose_confidence", "state", "module_state", "role") and v is not None}
                break
    except Exception as exc:  # noqa: BLE001 -- a frame without its numbers is still worth sending
        _log(f"{cid}: modules read failed ({type(exc).__name__}); sending the frame alone")
    return out


def post_plane(plane: str, token: str, payload: dict, *, dry_run: bool = False) -> dict:
    body = {"camera": payload["camera"], "captured_at": payload.get("captured_at"),
            "frame_hash": payload.get("frame_hash"), "readings": payload.get("readings") or {},
            "image_b64": base64.b64encode(payload["image"]).decode() if payload.get("image") else None}
    if dry_run:
        return {"dry_run": True, "bytes": len(payload.get("image") or b""), "readings": sorted(body["readings"])}
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"{plane.rstrip('/')}/api/frames/ingest", data=data, method="POST",
                                 headers={"Content-Type": "application/json", "X-Eaos-Wave-Token": token,
                                          "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read() or b"{}")


def once(cfg: dict, plane: str, viewer: str, work: str, *, only: set[str] | None = None, dry_run: bool = False) -> int:
    sent = 0
    for cam in (cfg.get("cameras") or []):
        if not isinstance(cam, dict) or not cam.get("id"):
            continue
        cid = str(cam["id"])
        if only and cid not in only:
            continue
        if cam.get("source", "edge") != "edge" or not cam.get("host"):
            continue                                   # nothing on the tailnet to read
        try:
            payload = read_box(cam, viewer)
        except urllib.error.HTTPError as e:
            _log(f"{cid}: the box answered HTTP {e.code}"); continue
        except Exception as e:  # noqa: BLE001
            _log(f"{cid}: the box did not answer ({type(e).__name__})"); continue
        try:
            out = post_plane(plane, work, payload, dry_run=dry_run)
        except urllib.error.HTTPError as e:
            _log(f"{cid}: the plane answered HTTP {e.code} — {e.read()[:200].decode(errors='replace')}"); continue
        except Exception as e:  # noqa: BLE001
            _log(f"{cid}: the plane did not answer ({type(e).__name__})"); continue
        sent += 1
        _log(f"{cid}: {len(payload.get('image') or b'')} bytes, readings {sorted(payload.get('readings') or {})} → {out}")
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plane", required=True, help="the work plane's base URL")
    ap.add_argument("--cameras", required=True, help="the plant's cameras.yaml")
    ap.add_argument("--camera", action="append", default=[], help="only this camera id (repeatable)")
    ap.add_argument("--once", action="store_true", help="one pass and exit (default: loop on the file's cadence)")
    ap.add_argument("--dry-run", action="store_true", help="read the boxes, post nothing")
    a = ap.parse_args(argv)

    import yaml
    with open(a.cameras, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    viewer = os.environ.get(str(cfg.get("token_env") or "EAOS_UII_VIEWER_TOKEN"), "").strip()
    work = (os.environ.get("EAOS_FRAMES_INGEST_TOKEN") or os.environ.get("EAOS_WORK_ACCESS_TOKEN") or "").strip()
    if not viewer:
        _log(f"no {cfg.get('token_env') or 'EAOS_UII_VIEWER_TOKEN'} in the environment; the boxes will refuse"); return 2
    every = _span_s(cfg.get("every"))
    only = set(a.camera) or None
    _log(f"plane {a.plane} · {len(cfg.get('cameras') or [])} camera(s) declared · every {int(every)}s"
         + (" · dry run" if a.dry_run else ""))
    while True:
        started = time.monotonic()
        n = once(cfg, a.plane, viewer, work, only=only, dry_run=a.dry_run)
        if a.once:
            return 0 if n else 1
        time.sleep(max(5.0, every - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())
