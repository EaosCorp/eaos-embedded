"""foam-region — the learned per-region foam classifier (ONNX), beside foam-cv.

foam_cv.py is the classical threshold detector (model id foam-cv/0.1.0). This
runs the distilled CNN trained in eaos-clients/dc-water/.../foam-cv/ on each
commissioned polygon region and reports a coverage *bucket* (0 none <5% ·
1 light 5-25% · 2 partial 25-50% · 3 most 50-90% · 4 full >90%), its
probabilities, and a colour class (white / brown / mixed). Both detectors
report on every frame so they can be compared before either is trusted alone.

Model files (UII_FOAM_MODEL_DIR, default /etc/eaos/models/foam-region):
  model.onnx   input "image" [n,3,224,224] float32 (ImageNet-normalised)
               outputs "coverage_cum_logits" [n,4], "colour_logits" [n,3]
  meta.json    model_id, input size/mean/std, buckets, colours, report

The crop must match training exactly: polygon mask, grey (114) outside, bbox
padded 4%, bilinear resize to 224. Kept in numpy + PIL; onnxruntime is the
only extra dependency and the module degrades to "unavailable" without it.
"""
from __future__ import annotations

import json
import os

import numpy as np

DEFAULT_DIR = "/etc/eaos/models/foam-region"
BUCKET_MID_PCT = [2.0, 15.0, 37.5, 70.0, 95.0]     # bucket centres, for a % estimate


class FoamRegionModel:
    def __init__(self, model_dir: str | None = None):
        self.dir = model_dir or os.environ.get("UII_FOAM_MODEL_DIR", DEFAULT_DIR)
        self.meta = None
        self.sess = None
        self.error = None
        try:
            with open(os.path.join(self.dir, "meta.json")) as f:
                self.meta = json.load(f)
            import onnxruntime as ort                      # noqa: WPS433
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(os.environ.get("UII_FOAM_THREADS", "2"))
            self.sess = ort.InferenceSession(os.path.join(self.dir, "model.onnx"),
                                             sess_options=so, providers=["CPUExecutionProvider"])
        except Exception as e:                             # missing files / no onnxruntime
            self.error = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return self.sess is not None

    @property
    def model_id(self) -> str:
        return (self.meta or {}).get("model_id", "foam-region/unknown")

    # ---- preprocessing (mirrors train_foam.region_crop + to_tensor) ----
    def _crop(self, rgb: np.ndarray, poly) -> np.ndarray:
        from PIL import Image, ImageDraw
        size = int(self.meta["input"]["size"])
        H, W = rgb.shape[0], rgb.shape[1]
        pts = [(float(x) * W, float(y) * H) for x, y in poly]
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        pad = 0.04
        pw, ph = (max(xs) - min(xs)) * pad, (max(ys) - min(ys)) * pad
        box = (max(0, int(min(xs) - pw)), max(0, int(min(ys) - ph)),
               min(W, int(max(xs) + pw)), min(H, int(max(ys) + ph)))
        img = Image.fromarray(np.ascontiguousarray(rgb[..., :3]).astype(np.uint8))
        mask = Image.new("L", (W, H), 0)
        ImageDraw.Draw(mask).polygon(pts, fill=255)
        grey = Image.new("RGB", (W, H), (114, 114, 114))
        out = Image.composite(img, grey, mask).crop(box).resize((size, size), Image.BILINEAR)
        a = np.asarray(out).astype(np.float32) / 255.0
        a = (a - np.array(self.meta["input"]["mean"], np.float32)) / np.array(self.meta["input"]["std"], np.float32)
        return a.transpose(2, 0, 1)

    def predict(self, rgb: np.ndarray, polygons: dict) -> dict:
        """polygons: {name: [[x, y], ...]} fractions. Returns {name: result}."""
        if not self.available or not polygons:
            return {}
        names = list(polygons)
        batch = np.stack([self._crop(rgb, polygons[n]) for n in names]).astype(np.float32)
        cum, col = self.sess.run(None, {"image": batch})
        p_gt = 1.0 / (1.0 + np.exp(-cum))                 # P(coverage > k), k = 0..3
        colp = np.exp(col - col.max(1, keepdims=True)); colp /= colp.sum(1, keepdims=True)
        buckets = self.meta["coverage_buckets"]; colours = self.meta["colours"]
        out = {}
        for i, n in enumerate(names):
            b = int((p_gt[i] > 0.5).sum())
            # bucket probabilities from the cumulative curve
            pk = np.concatenate([[1.0], p_gt[i]]); pk1 = np.concatenate([p_gt[i], [0.0]])
            probs = np.clip(pk - pk1, 0.0, 1.0); probs = probs / max(probs.sum(), 1e-6)
            est_pct = float(np.dot(probs, BUCKET_MID_PCT))
            ci = int(colp[i].argmax())
            out[n] = {"bucket": b, "bucket_label": buckets[b], "coverage_est_pct": round(est_pct, 1),
                      "bucket_probs": [round(float(x), 3) for x in probs],
                      "confidence": round(float(probs[b]), 3),
                      "colour": colours[ci] if b > 0 else "white",
                      "colour_confidence": round(float(colp[i][ci]), 3)}
        return out
