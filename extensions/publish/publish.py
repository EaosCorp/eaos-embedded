"""The publisher thread: one outward POST per camera per cadence.

WHAT GOES OVER THE LINK, and why it is shaped this way. The frame is ~150 KB and the readings are
a few dozen bytes. The readings move every cycle; the picture often does not. So the readings go
every cycle and the FRAME goes only when its hash changed since the last one accepted. The plane
carries the stored frame forward when a push has readings alone, and ages the two separately: how
old the picture is, and how old the measurement is. A watch ages on the measurement.

At the default cadence that is about 96 pushes a day per camera and at most 96 frames, so roughly
15 MB/day/camera worst case and much less when the basin is still.

The thread never dies. A box that cannot reach the plane keeps its frames and its evidence and
tries again next cycle; nothing here is the record, so nothing is lost by failing.
"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request

_TIMEOUT = 45.0
_MIN_EVERY_S = 30.0


class Publisher(threading.Thread):
    def __init__(self, hub, cfg: dict) -> None:
        super().__init__(daemon=True, name="uii-publish")
        self.hub = hub
        self.store = hub.store
        self.url = str(cfg["url"])
        self.every_s = max(float(cfg.get("every_s") or 900), _MIN_EVERY_S)
        self.max_w = int(cfg.get("max_w") or 1280)
        self.quality = int(cfg.get("quality") or 70)
        self.modules = dict(cfg.get("modules") or {})       # module id -> plane camera id
        self.token_file = cfg.get("token_file") or "/etc/eaos/frames-ingest-token"
        self.token_env = cfg.get("token_env") or "EAOS_FRAMES_INGEST_TOKEN"
        self.stop = threading.Event()
        self._sent_hash: dict[str, str] = {}                # module id -> the last frame hash accepted
        self._blobs = None

    # -- what the box knows -------------------------------------------------

    def blobs(self):
        """The vision piece's frame store, found the way it builds it. None before vision is up."""
        if self._blobs is None:
            try:
                from uii_vision.blobstore import FrameBlobStore
            except ModuleNotFoundError:               # the box's layout: extensions/vision/
                from extensions.vision.blobstore import FrameBlobStore
            frames_dir = os.environ.get("UII_FRAMES_DIR") or os.path.join(
                self.hub.config.data_dir, "frames")
            self._blobs = FrameBlobStore(frames_dir)
        return self._blobs

    def _token(self) -> str:
        env = os.environ.get(self.token_env, "").strip()
        if env:
            return env
        try:
            with open(self.token_file, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _module_ids(self) -> list[str]:
        if self.modules:
            return list(self.modules)
        seen = []
        for s in (getattr(self.hub.southbound, "sessions", None) or {}).values():
            mid = getattr(s, "module_id", None)
            if mid and mid not in seen:
                seen.append(mid)
        return seen

    def _readings(self, mid: str) -> dict:
        """The same fields `/v1/modules` shows for this module -- the box's own models' word."""
        out: dict = {}
        obs = self.store.latest_observation(mid, "foam_coverage")
        if obs:
            d = obs.get("data") or {}
            out["foam_coverage_pct"] = d.get("value")
            out["foam_type"] = d.get("foam_type")
            out["foam_permitted_use"] = (obs.get("quality") or {}).get("permitted_use")
            out["readings_at"] = obs.get("time")
        iq = self.store.latest_observation(mid, "image_quality")
        if iq:
            out["image_quality"] = (iq.get("data") or {}).get("value")
        dose = self.store.latest_observation(mid, "bfp_dose")
        if dose:
            d = dose.get("data") or {}
            out["dose_class"] = d.get("class")
            out["dose_confidence"] = d.get("confidence")
        return {k: v for k, v in out.items() if v is not None}

    def _frame(self, mid: str) -> tuple[bytes | None, str | None, str | None]:
        """(jpeg, hash, captured_at) for the module's newest frame, squeezed for the link."""
        obs = self.store.latest_observation(mid, "frames")
        digest = ((obs or {}).get("data") or {}).get("frame_hash")
        if not digest:
            return None, None, None
        raw = self.blobs().get(digest)
        if not raw:
            return None, digest, (obs or {}).get("time")
        try:
            from uii_vision.framesource import recompress
        except ModuleNotFoundError:
            from extensions.vision.framesource import recompress
        try:
            out = recompress(raw, max_w=self.max_w, quality=self.quality)
        except Exception:                             # noqa: BLE001 -- send the original rather than nothing
            out = raw
        return out, digest, (obs or {}).get("time")

    # -- the loop -----------------------------------------------------------

    def run(self) -> None:
        # a first pass shortly after boot, then on the cadence
        if self.stop.wait(5.0):
            return
        while True:
            started = time.monotonic()
            try:
                self.tick()
            except Exception as e:                    # noqa: BLE001 -- the publisher must never die
                self._log(f"tick failed: {type(e).__name__}: {str(e)[:160]}")
            if self.stop.wait(max(5.0, self.every_s - (time.monotonic() - started))):
                return

    def tick(self) -> int:
        token = self._token()
        if not token:
            self._log(f"no ingest token ({self.token_env} or {self.token_file}); nothing is sent")
            return 0
        sent = 0
        for mid in self._module_ids():
            camera = self.modules.get(mid, mid)
            body, digest, shot = self._frame(mid)
            readings = self._readings(mid)
            if body is None and not readings:
                continue                              # this module has nothing to say yet
            fresh = bool(digest) and self._sent_hash.get(mid) != digest
            payload = {"camera": camera, "captured_at": readings.get("readings_at") or shot,
                       "frame_hash": digest, "readings": readings,
                       "image_b64": base64.b64encode(body).decode() if (fresh and body) else None}
            try:
                self._post(payload, token)
            except urllib.error.HTTPError as e:
                self._log(f"{camera}: the plane answered HTTP {e.code}")
                continue
            except Exception as e:                    # noqa: BLE001
                self._log(f"{camera}: the plane did not answer ({type(e).__name__})")
                continue
            if fresh:
                self._sent_hash[mid] = digest
            sent += 1
        return sent

    def _post(self, payload: dict, token: str) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(self.url, data=data, method="POST", headers={
            "Content-Type": "application/json",
            "X-Eaos-Wave-Token": token, "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            return json.loads(r.read() or b"{}")

    def _log(self, msg: str) -> None:
        try:
            self.store.append("event", {"event": "publish", "detail": msg},
                              "urn:uii:schema:event:0.1", actor="system:publish")
        except Exception:                             # noqa: BLE001
            pass
        print(f"[publish] {msg}", flush=True)
