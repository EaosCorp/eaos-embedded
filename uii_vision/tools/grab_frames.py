"""Pull the latest frame from each site camera, optionally restamp it, render a sheet.

    python3 -m uii_vision.tools.grab_frames --site dcwa-blue --render

The hub API already serves frames small (`?w=&q=`) precisely so a pull over cellular is
cheap; this tool never asks for `?full=1` unless told to, and fetches ONE frame per camera
per run. It is not a downloader — `retain.py` and `curate_frames.py` are for pulling sets,
and they run ON the box where the transfer is free.

**Restamping.** `--restamp` paints over the camera's burned-in OSD clock and writes a
different date in the same place, for building a worked example against a past event. The
frame's real provenance is never discarded: every image gets a `<name>.json` sidecar
carrying the true capture time, the frame hash the box served, and what the visible stamp
was changed to. A restamped frame is illustrative material, not evidence — the sheet says
so on every tile, and the sidecar is how anyone downstream can tell.

Tokens are never stored here. `UII_API_TOKEN`, or `--token-file` (default
`~/.eaos/uii-api-token`). A viewer token is enough; this tool only reads.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# Site inventories. Address + module only — no credentials, no camera passwords.
# (DC Water hosts/roles: vault 01-company/credentials/dc-water/dcwa-blue-plains-edge-computers.md)
SITES: dict[str, dict] = {
    "dcwa-blue": {
        # live: the hub API on each edge box (Reolink/GoPro frames as the box holds them now)
        "cameras": [
            {"module": "campod-01", "host": "100.124.44.125", "box": "dcwa-blu-ec5350-01",
             "label": "Aeration basin — east", "role": "aeration-foam-cam", "osd": True},
            {"module": "campod-02", "host": "100.66.88.44", "box": "dcwa-blu-ec5350-02",
             "label": "Aeration basin — west", "role": "aeration-foam-cam", "osd": True},
            {"module": "campod-03", "host": "100.121.15.100", "box": "dcwa-blu-rpi-01",
             "label": "Belt filter press", "role": "bfp-dose-cam", "osd": False},
        ],
        # archive: where the BFP chain SENDS its frames (blob `images`, beside Cosmos
        # CvMetadataDB/images). Path convention from bfp_publish.py and the legacy uploader:
        #   <EQUIPMENT>/<YYYY>/<MM>/<DD>/<EQUIPMENT>_<yy-mm-dd-HH-MM-SS>_1600.jpg
        # The basin cameras publish nothing — their frames are pulled from the box by digest.
        "published": {
            "container": "images",
            "equipment": [
                {"id": "A4", "label": "Belt filter press A4", "role": "bfp-dose-cam", "osd": False},
                {"id": "A2", "label": "Belt filter press A2", "role": "bfp-dose-cam", "osd": False},
            ],
        },
    },
}

# The Reolink RLC-811A burns its clock top-centre. Fractions of the frame, so the box lands
# in the right place whatever width was fetched. Measured on 960x540 pulls: x 379-581, y 0-24.
OSD_BOX = (0.395, 0.0, 0.605, 0.045)
OSD_FORMAT = "%d/%m/%Y %H:%M:%S %a"       # 24/08/2026 14:45:08 MON — the camera's own format


def _get(url: str, token: str, timeout: int = 30) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), {k.lower(): v for k, v in r.headers.items()}


def fetch(cam: dict, out_dir: Path, token: str, width: int, quality: int, full: bool) -> dict:
    """One frame from one camera. Returns a provenance record (never raises on a dead camera)."""
    q = "?full=1" if full else f"?w={width}&q={quality}"
    url = f"http://{cam['host']}:8400/v1/modules/{cam['module']}/frame{q}"
    rec = {"module": cam["module"], "box": cam["box"], "role": cam["role"],
           "label": cam["label"], "osd": cam.get("osd", False), "source": "edge",
           "url": url, "ok": False}
    try:
        body, hdr = _get(url, token)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read()).get("detail", "")
        except Exception:  # noqa: BLE001
            pass
        rec["error"] = f"HTTP {e.code}{': ' + detail if detail else ''}"
        return rec
    except Exception as e:  # noqa: BLE001 — a box on cellular drops out; report, do not abort the run
        rec["error"] = f"{type(e).__name__}: {e}"
        return rec
    path = out_dir / f"{cam['module']}.jpg"
    path.write_bytes(body)
    rec.update(ok=True, file=path.name, bytes=len(body),
               captured_at=hdr.get("x-frame-time", ""), frame_hash=hdr.get("x-frame-hash", ""),
               fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    return rec


BLOB_NAME_TIME = "%y-%m-%d-%H-%M-%S"      # A4_26-08-19-18-30-15_1600.jpg


def _complete(body: bytes, max_flat_tail: float = 0.05) -> bool:
    """Is this a whole frame, or an upload that stopped part-way?

    Two failure modes, and only the first one raises. A badly truncated JPEG fails to decode
    at all. A *slightly* truncated one decodes fine and fills every missing scan line with
    flat grey — the A2 uploads land like this, 93% grey below a sliver of real belt — so
    decoding is not the test. The test is whether the bottom of the image carries any
    variation: real frames measure 0% flat tail, a part-written one 90%+."""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(body))
        im.load()
    except Exception:  # noqa: BLE001
        return False
    g = im.convert("L")
    w, h = g.size
    if h < 20:
        return True
    flat_tail = 0
    for i in range(min(64, h)):                     # sample rows upward from the bottom
        y = h - 1 - int(i * h / 64)
        lo, hi = g.crop((0, y, w, y + 1)).getextrema()
        if hi - lo > 3:
            break
        flat_tail += 1
    return (flat_tail / 64) <= max_flat_tail


def _exif_time(path: Path) -> str:
    """The camera's own DateTimeOriginal, when the frame carries EXIF. Kept beside the
    name-derived time rather than replacing it: on the legacy uploads the two disagree by the
    site's UTC offset, and guessing which is which would put a wrong zone on the record."""
    try:
        from PIL import Image
        exif = Image.open(path).getexif()
        for tag in (36867, 306):              # DateTimeOriginal, DateTime
            if exif.get(tag):
                return datetime.strptime(str(exif[tag]), "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:  # noqa: BLE001
        pass
    return ""


def fetch_published(eq: dict, container: str, out_dir: Path, conn_str: str,
                    max_walk_back: int = 12, max_days: int = 30) -> dict:
    """The newest frame this equipment has SENT, from the blob archive.

    Walks YYYY/MM/DD taking the highest branch at each level — four listings instead of
    enumerating a container that holds years of frames. This is cloud-to-laptop, so it does
    not touch the edge box and spends none of its cellular budget."""
    rec = {"module": eq["id"], "box": f"blob:{container}", "role": eq["role"],
           "label": eq["label"], "osd": eq.get("osd", False), "source": "published", "ok": False}
    try:
        from azure.storage.blob import BlobServiceClient
        cc = BlobServiceClient.from_connection_string(conn_str).get_container_client(container)

        def subdirs(p: str) -> list[str]:
            return sorted(b.name for b in cc.walk_blobs(name_starts_with=p, delimiter="/")
                          if b.name.endswith("/"))

        def days_newest_first(limit: int):
            """YYYY/MM/DD prefixes, newest first, stepping back through months and years.

            A whole day can be unusable — A2's last publishing day uploaded nothing but
            truncated frames — so stopping at the newest day would report the equipment as
            having no image at all when it has plenty, a fortnight earlier."""
            n = 0
            for year in reversed(subdirs(f"{eq['id']}/")):
                for month in reversed(subdirs(year)):
                    for day in reversed(subdirs(month)):
                        yield day
                        n += 1
                        if n >= limit:
                            return

        blob = data = None
        skipped: list[str] = []
        searched: list[str] = []
        # "the latest frame we have" must mean the latest USABLE one: uploads over a flaky
        # link get cut off part-way, and a broken newest frame must not shadow a good one.
        for day in days_newest_first(max_days):
            searched.append(day)
            blobs = sorted(cc.list_blobs(name_starts_with=day), key=lambda b: b.name)
            for cand in reversed(blobs[-max_walk_back:]):
                body = cc.download_blob(cand.name).readall()
                if _complete(body):
                    blob, data = cand, body
                    break
                skipped.append(f"{Path(cand.name).name} ({len(body) // 1024} KB, incomplete)")
            if blob is not None:
                break
        if blob is None:
            rec["error"] = (f"no complete frame in {len(searched)} day(s) back from "
                            f"{searched[0] if searched else eq['id'] + '/'} "
                            f"({len(skipped)} truncated)")
            rec["skipped_broken"] = skipped[:10]
            return rec
        if skipped:
            rec["skipped_broken"] = skipped
            rec["days_searched"] = searched
    except Exception as e:  # noqa: BLE001
        rec["error"] = f"{type(e).__name__}: {str(e)[:120]}"
        return rec
    path = out_dir / f"{eq['id']}.jpg"
    path.write_bytes(data)
    # capture time comes from the blob NAME (when the frame was taken); last_modified is
    # merely when the uploader got round to it, and the two differ by the publish interval.
    captured = ""
    stem = Path(blob.name).stem
    parts = stem.split("_")
    if len(parts) >= 2:
        try:
            captured = datetime.strptime(parts[1], BLOB_NAME_TIME).isoformat(timespec="seconds")
        except ValueError:
            pass
    rec.update(ok=True, file=path.name, bytes=len(data), blob=blob.name,
               captured_at=captured or blob.last_modified.isoformat(),
               exif_datetime=_exif_time(path),
               uploaded_at=blob.last_modified.isoformat(),
               fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    return rec


def restamp(path: Path, when: datetime) -> None:
    """Paint over the burned-in OSD clock and write `when` in its place.

    The cover is built per row from the pixels immediately left and right of the box, so it
    blends into whatever is behind the stamp — open sky on one camera, a building roofline on
    another — instead of leaving a flat rectangle."""
    from PIL import Image, ImageDraw, ImageFont

    im = Image.open(path).convert("RGB")
    w, h = im.size
    x0, y0, x1, y1 = (int(OSD_BOX[0] * w), int(OSD_BOX[1] * h),
                      int(OSD_BOX[2] * w), int(OSD_BOX[3] * h))
    y1 = max(y1, y0 + 14)
    px = im.load()
    pad = 12
    for y in range(y0, y1):
        left = [px[x, y] for x in range(max(0, x0 - pad), x0)]
        right = [px[x, y] for x in range(x1, min(w, x1 + pad))]
        ref = left + right or [(90, 120, 160)]
        med = tuple(sorted(c[i] for c in ref)[len(ref) // 2] for i in range(3))
        for x in range(x0, x1):
            px[x, y] = med

    text = when.strftime(OSD_FORMAT).upper()
    size = max(11, int((y1 - y0) * 0.78))
    font = None
    for candidate in ("/System/Library/Fonts/Supplemental/Andale Mono.ttf",
                      "/System/Library/Fonts/SFNSMono.ttf",
                      "/System/Library/Fonts/Supplemental/Courier New Bold.ttf"):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, size)
            break
    if font is None:
        font = ImageFont.load_default()
    d = ImageDraw.Draw(im)
    tw = d.textlength(text, font=font)
    tx, ty = x1 - tw, y0 + max(0, ((y1 - y0) - size) // 2)
    # the camera draws white glyphs with a thin dark edge so they read over sky or concrete
    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        d.text((tx + dx, ty + dy), text, font=font, fill=(40, 40, 40))
    d.text((tx, ty), text, font=font, fill=(255, 255, 255))
    im.save(path, quality=88)


def render(records: list[dict], out: Path, title: str) -> Path:
    """A contact sheet: every tile carries what it is, when it was really taken, and whether
    the visible clock was changed."""
    tiles = []
    for r in records:
        if not r.get("ok"):
            tiles.append(
                f"""<figure class="tile missing">
  <div class="ph">no frame</div>
  <figcaption><b>{r['label']}</b><span class="mod">{r['module']} · {r['box']}</span>
  <span class="err">{r.get('error', 'unavailable')}</span></figcaption>
</figure>""")
            continue
        shown = r.get("display_time") or r.get("captured_at", "")
        if r.get("restamped_to"):
            mark = ('<span class="restamped">clock overwritten for illustration · '
                    f'really captured {r["captured_at"]}</span>')
        elif r.get("dated_to"):
            # no clock in the frame to overwrite, so only the caption moved
            mark = ('<span class="restamped">dated for illustration · no clock in this frame · '
                    f'really captured {r["captured_at"]}</span>')
        else:
            mark = '<span class="asis">as captured</span>'
        origin = (f"blob {r['blob']}" if r.get("source") == "published"
                  else f"frame {r.get('frame_hash', '')[:16]}…")
        tiles.append(
            f"""<figure class="tile">
  <img src="{r['file']}" alt="{r['label']}">
  <figcaption><b>{r['label']}</b><span class="mod">{r['module']} · {r['box']} · {r['role']}</span>
  <span class="t">{shown}</span>{mark}
  <span class="hash">{origin}</span></figcaption>
</figure>""")
    html = f"""<!doctype html>
<meta charset="utf-8"><title>{title}</title>
<style>
 :root {{ --bg:#0f1113; --card:#171a1d; --fg:#e8e6e3; --dim:#9aa0a6; --warn:#e0a35c; }}
 body {{ background:var(--bg); color:var(--fg); font:14px/1.5 -apple-system,Segoe UI,sans-serif; margin:0; padding:32px; }}
 h1 {{ font-size:18px; font-weight:600; margin:0 0 4px; }}
 .sub {{ color:var(--dim); margin:0 0 24px; }}
 .grid {{ display:grid; gap:20px; grid-template-columns:repeat(auto-fill,minmax(380px,1fr)); }}
 .tile {{ margin:0; background:var(--card); border-radius:10px; overflow:hidden; }}
 .tile img {{ width:100%; display:block; }}
 .ph {{ aspect-ratio:16/9; display:grid; place-items:center; color:var(--dim); background:#101215; }}
 figcaption {{ padding:12px 14px; display:flex; flex-direction:column; gap:3px; }}
 .mod,.hash {{ color:var(--dim); font-size:12px; font-family:ui-monospace,monospace; }}
 .t {{ font-family:ui-monospace,monospace; font-size:13px; }}
 .restamped {{ color:var(--warn); font-size:12px; }}
 .asis {{ color:var(--dim); font-size:12px; }}
 .err {{ color:#d98080; font-size:12px; }}
</style>
<h1>{title}</h1>
<p class="sub">Blue Plains site cameras · pulled {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}</p>
<div class="grid">
{chr(10).join(tiles)}
</div>
"""
    out.write_text(html)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--site", default="dcwa-blue", choices=sorted(SITES))
    p.add_argument("--source", default="both", choices=("edge", "published", "both"),
                   help="edge = the box's current frame; published = the newest frame the "
                        "equipment has SENT to the blob archive; both (default)")
    p.add_argument("--camera", action="append", help="module or equipment id; repeatable (default: all)")
    p.add_argument("--out", type=Path, default=Path("frames"))
    p.add_argument("--width", type=int, default=960)
    p.add_argument("--quality", type=int, default=60)
    p.add_argument("--full", action="store_true", help="the stored frame (~200 KB) — opt in, costs cellular")
    p.add_argument("--restamp", metavar="'YYYY-MM-DD HH:MM[:SS]'",
                   help="overwrite the burned-in camera clock with this time")
    p.add_argument("--label", action="append", metavar="MODULE=TEXT",
                   help="rename a camera in the sheet, e.g. campod-03='Belt filter press 3'")
    p.add_argument("--render", nargs="?", const="index.html", metavar="FILE",
                   help="write a contact sheet next to the frames")
    p.add_argument("--title", default="Blue Plains — site cameras")
    p.add_argument("--no-fetch", action="store_true", help="render from frames already on disk")
    p.add_argument("--token-file", type=Path, default=Path.home() / ".eaos" / "uii-api-token")
    p.add_argument("--env-file", type=Path, default=Path.home() / ".eaos" / "dcwater-cv.env",
                   help="file holding AZURE_STORAGE_CONNECTION_STRING for the published archive")
    a = p.parse_args(argv)

    site = SITES[a.site]
    cams = list(site["cameras"]) if a.source in ("edge", "both") else []
    pubs = list((site.get("published") or {}).get("equipment", [])) if a.source in ("published", "both") else []
    if a.camera:
        want = set(a.camera)
        cams = [c for c in cams if c["module"] in want]
        pubs = [e for e in pubs if e["id"] in want]
        if not cams and not pubs:
            print(f"nothing matched {sorted(want)}", file=sys.stderr)
            return 2
    labels = dict(kv.split("=", 1) for kv in (a.label or []))
    cams = [{**c, "label": labels.get(c["module"], c["label"])} for c in cams]
    pubs = [{**e, "label": labels.get(e["id"], e["label"])} for e in pubs]

    when = None
    if a.restamp:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
            try:
                when = datetime.strptime(a.restamp, fmt)
                break
            except ValueError:
                continue
        if when is None:
            print(f"could not read --restamp {a.restamp!r} (use 'YYYY-MM-DD HH:MM')", file=sys.stderr)
            return 2

    a.out.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    def _date_it(r: dict) -> None:
        """Apply --restamp. A frame with a burned-in clock gets the pixels rewritten; one
        without (the GoPro presses) only gets the caption moved — painting the Reolink OSD
        box onto a frame that has no OSD would stamp a rectangle over the belt."""
        if when is None or not r.get("ok"):
            return
        if r.get("osd"):
            restamp(a.out / r["file"], when)
            r["restamped_to"] = when.isoformat(timespec="seconds")
            r["note"] = ("the visible clock in this image was overwritten for illustration; "
                         "captured_at is the real capture time")
        else:
            r["dated_to"] = when.isoformat(timespec="seconds")
            r["note"] = ("this frame carries no visible clock; it is PRESENTED as the dated time "
                         "but captured_at is when it was really taken")
        r["display_time"] = when.strftime(OSD_FORMAT).upper()

    def _report(r: dict, name: str, label: str) -> None:
        (a.out / f"{name}.json").write_text(json.dumps(r, indent=2))
        records.append(r)
        status = (f"{r['bytes'] / 1024:.0f} KB  {r.get('captured_at', '')}"
                  if r["ok"] else r.get("error", "failed"))
        print(f"  {name:<11} {label:<26} {status}")

    if a.no_fetch:
        for c in cams + [{**e, "module": e["id"]} for e in pubs]:
            side = a.out / f"{c['module']}.json"
            records.append(json.loads(side.read_text()) if side.is_file()
                           else {**c, "ok": False, "error": "not fetched yet"})
    else:
        if cams:
            token = os.environ.get("UII_API_TOKEN", "").strip()
            if not token and a.token_file.is_file():
                token = a.token_file.read_text().strip()
            if not token:
                print(f"no token: set UII_API_TOKEN or write one to {a.token_file}", file=sys.stderr)
                return 2
            print("edge (live on the box):")
            for c in cams:
                r = fetch(c, a.out, token, a.width, a.quality, a.full)
                _date_it(r)
                _report(r, c["module"], c["label"])
        if pubs:
            conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "").strip()
            if not conn and a.env_file.is_file():
                for line in a.env_file.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("AZURE_STORAGE_CONNECTION_STRING="):
                        conn = line.split("=", 1)[1].strip().strip("'\"")
            if not conn:
                print(f"no storage credential: set AZURE_STORAGE_CONNECTION_STRING or write "
                      f"{a.env_file}", file=sys.stderr)
                return 2
            container = (SITES[a.site].get("published") or {}).get("container", "images")
            print("published (newest frame sent to the archive):")
            for e in pubs:
                r = fetch_published(e, container, a.out, conn)
                _date_it(r)
                _report(r, e["id"], e["label"])

    if a.render:
        out = a.out / a.render if not Path(a.render).is_absolute() else Path(a.render)
        render(records, out, a.title)
        print(f"\nsheet: {out}")
    got = sum(1 for r in records if r.get("ok"))
    print(f"{got}/{len(records)} camera(s) returned a frame")
    return 0 if got else 1


if __name__ == "__main__":
    raise SystemExit(main())
