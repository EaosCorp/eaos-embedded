"""Frame sources for campod, and the sim rule.

Two real jobs and one bench job behind one tiny interface:

  grab() -> (rgb ndarray HxWx3 uint8, meta dict)

* HTTPSnapshotSource — pulls a still from the Reolink over the HTTP API
  (cmd=Snap), the same call we verified on the edge boxes. One frame per
  grab is exactly the "almost telemetry" cadence foam monitoring needs; a
  full RTSP decode path (opencv) is a later add for high-rate work.
* SimFrameSource — synthetic basin scene with a controllable foam fraction.
  UII_SIM=1 selects it. campod and the interpreter are byte-identical
  against sim and real (architecture §7): a foam model proven on recorded /
  synthetic frames ships by git tag, never as an edit on the box.

PNG encode/decode go through Pillow (the extension's imaging dependency);
the CV itself is pure numpy.
"""
from __future__ import annotations

import io
import os
import urllib.parse
import urllib.request

import numpy as np


# ---- encode / decode (Pillow) ------------------------------------------------
# Frames are stored as JPEG, not PNG: the edge boxes are on cellular, so every
# byte that might be transferred is a cost. JPEG at q85 is a fraction of PNG and
# more than good enough for coverage CV (a fraction of the surface, not pixels).

def encode_jpeg(rgb: np.ndarray, quality: int = 85) -> bytes:
    from PIL import Image
    arr = np.asarray(rgb)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    buf = io.BytesIO()
    Image.fromarray(arr[:, :, :3], "RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def decode(data: bytes) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def downscale(rgb: np.ndarray, max_w: int | None) -> np.ndarray:
    """Cap width at max_w (keeps aspect). Coverage is scale-invariant, so this
    shrinks storage, CV cost, and any full-frame transfer with no accuracy loss."""
    arr = np.asarray(rgb)
    h, w = arr.shape[0], arr.shape[1]
    if not max_w or w <= max_w:
        return arr
    from PIL import Image
    new_h = int(round(h * max_w / w))
    im = Image.fromarray(arr.astype(np.uint8)[:, :, :3], "RGB").resize(
        (max_w, new_h), Image.BILINEAR)
    return np.asarray(im)


def recompress(data: bytes, max_w: int | None = None, quality: int = 70) -> bytes:
    """Decode a stored frame and re-encode small — the transfer-time squeeze for
    API image fetches over cellular. Default 70 is visibly fine and cheap."""
    from PIL import Image
    im = Image.open(io.BytesIO(data)).convert("RGB")
    if max_w and im.width > max_w:
        nh = int(round(im.height * max_w / im.width))
        im = im.resize((max_w, nh), Image.BILINEAR)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---- sim ---------------------------------------------------------------------

class SimFrameSource:
    """Dark water with bright foam patches covering ~coverage_pct of the frame.
    Deterministic per frame index; `coverage_pct` and `foam_type` are settable
    so a test can drive the hidden truth the CV must recover."""

    def __init__(self, w=320, h=180, coverage_pct=25.0, foam_type="nuisance_white",
                 quality=1.0, seed=1):
        self.w, self.h = w, h
        self.coverage_pct = coverage_pct
        self.foam_type = foam_type
        self.quality = quality       # 1.0 sharp/lit; lower = darker/blurrier
        self.i = 0
        self._seed = seed

    def set(self, coverage_pct=None, foam_type=None, quality=None):
        if coverage_pct is not None: self.coverage_pct = coverage_pct
        if foam_type is not None: self.foam_type = foam_type
        if quality is not None: self.quality = quality

    def grab(self, command=None):
        rng = np.random.default_rng(self._seed + self.i)
        self.i += 1
        h, w = self.h, self.w
        # water: dark, slightly green, moderately saturated
        img = np.zeros((h, w, 3), dtype=np.float32)
        img[..., 0] = 0.10; img[..., 1] = 0.16; img[..., 2] = 0.12
        img += rng.normal(0, 0.01, img.shape)
        # paint foam rows from the top to hit the target coverage fraction
        frac = max(0.0, min(1.0, self.coverage_pct / 100.0))
        rows = int(round(frac * h))
        if rows > 0:
            if self.foam_type == "biological_brown":
                fg = np.array([0.62, 0.44, 0.28])    # warm tan, moderate sat
            else:
                fg = np.array([0.92, 0.93, 0.94])    # bright near-white
            img[:rows, :, :] = fg
            img[:rows, :, :] += rng.normal(0, 0.02, (rows, w, 3))
        # quality knob: dim + blur when < 1
        q = float(self.quality)
        img *= (0.25 + 0.75 * q)
        if q < 0.6:  # cheap blur = 3x3 box via shifts
            img = (img + np.roll(img, 1, 0) + np.roll(img, -1, 0) +
                   np.roll(img, 1, 1) + np.roll(img, -1, 1)) / 5.0
        rgb = np.clip(img * 255.0, 0, 255).astype(np.uint8)
        meta = {"source": "sim", "w": w, "h": h, "exposure": round(q, 3),
                "true_coverage_pct": self.coverage_pct, "frame_index": self.i}
        return rgb, meta


# ---- real Reolink HTTP snapshot ---------------------------------------------

class HTTPSnapshotSource:
    """Reolink HTTP API still capture (cmd=Snap). Reads host/user/password
    from the on-device credential env already provisioned at
    /etc/eaos/camera-credentials.env."""

    def __init__(self, host, user, password, timeout=12):
        self.host, self.user, self.password, self.timeout = host, user, password, timeout

    @classmethod
    def from_env(cls, env=None):
        e = env or os.environ
        return cls(e.get("CAMERA_HOST", "192.168.3.160"),
                   e.get("CAMERA_USER", "admin"),
                   e.get("CAMERA_PASSWORD", ""))

    def grab(self, command=None):
        q = urllib.parse.urlencode({
            "cmd": "Snap", "channel": 0, "rs": "campod",
            "user": self.user, "password": self.password})
        url = f"http://{self.host}/cgi-bin/api.cgi?{q}"
        with urllib.request.urlopen(url, timeout=self.timeout) as r:
            data = r.read()
        rgb = decode(data)
        h, w = rgb.shape[0], rgb.shape[1]
        return rgb, {"source": "reolink-http", "w": w, "h": h}


class GoProSnapshotSource:
    """Open GoPro HTTP capture: health-check the SD card, fire the shutter,
    download the newest photo, delete it from the camera. The sequence is the
    proven one from gopro-capture-v3.sh on the Blue Plains Pi (status fields
    33/54/111/117 = storage state / remaining KB / write-speed error /
    capacity KB). A grab raises RuntimeError with a health reason when the
    camera or its SD card is not usable; campod treats that as a failed grab,
    never a crash, so a dead camera just means no frames until it is fixed."""

    def __init__(self, host="10.5.5.9", port=8080, timeout=20):
        self.host, self.port, self.timeout = host, int(port), timeout
        self.base = f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(cls, env=None):
        e = env or os.environ
        return cls(e.get("CAMERA_HOST", "10.5.5.9"), e.get("GOPRO_PORT", "8080"))

    def _json(self, path):
        import json as _json
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
            return _json.loads(r.read().decode())

    def _health(self):
        st = self._json("/gopro/camera/state").get("status", {})
        storage, remaining = int(st.get("33", -1)), int(st.get("54", 0))
        wr_err, capacity = int(st.get("111", 1)), int(st.get("117", 0))
        if storage != 0:
            raise RuntimeError(f"gopro-health: storage status={storage} (SD missing/error)")
        if remaining <= 0 or capacity <= 0:
            raise RuntimeError(f"gopro-health: invalid SD capacity (remaining_kb={remaining}, capacity_kb={capacity})")
        if wr_err:
            raise RuntimeError("gopro-health: SD write-speed error (status.111)")

    def _newest(self):
        media = self._json("/gopro/media/list").get("media", [])
        latest = None
        for d in media:
            for f in d.get("fs", []):
                key = (int(f.get("cre", 0)), f.get("n", ""))
                if latest is None or key > (latest[0], latest[1]):
                    latest = (key[0], f.get("n", ""), d.get("d", ""))
        return latest

    def grab(self, command=None):
        import time as _time
        self._health()
        before = self._newest()
        with urllib.request.urlopen(self.base + "/gopro/camera/shutter/start", timeout=self.timeout) as r:
            r.read()
        photo = None
        for _ in range(20):                    # up to ~20 s for the file to land
            _time.sleep(1.0)
            latest = self._newest()
            if latest and latest != before:
                photo = latest; break
        if not photo:
            raise RuntimeError("gopro: shutter fired but no new media appeared")
        _, name, dirname = photo
        with urllib.request.urlopen(f"{self.base}/videos/DCIM/{dirname}/{name}", timeout=60) as r:
            data = r.read()
        try:
            urllib.request.urlopen(f"{self.base}/gopro/media/delete/file?path={dirname}/{name}",
                                   timeout=self.timeout).read()
        except OSError:
            pass                               # a leftover file is cleaned next health cycle
        rgb = decode(data)
        return rgb, {"source": "gopro-http", "w": rgb.shape[1], "h": rgb.shape[0],
                     "camera_file": f"{dirname}/{name}"}


def make_source(env=None):
    """UII_SIM=1 -> sim; CAMERA_KIND=gopro -> GoPro; else Reolink snapshot."""
    e = env or os.environ
    if e.get("UII_SIM") in ("1", "true", "yes"):
        return SimFrameSource(coverage_pct=float(e.get("UII_SIM_COVERAGE", 25.0)),
                              foam_type=e.get("UII_SIM_FOAM", "nuisance_white"),
                              quality=float(e.get("UII_SIM_QUALITY", 1.0)))
    if e.get("CAMERA_KIND", "reolink").lower() == "gopro":
        return GoProSnapshotSource.from_env(e)
    return HTTPSnapshotSource.from_env(e)
