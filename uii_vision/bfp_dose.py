"""bfp-dose — whole-frame belt-filter-press polymer dose classifier (ONNX).

The dcwater-cv EfficientNet-B0 (Good / Overdose / Underdose, curriculum-
trained on expert survey labels) exported to ONNX and run on the capture
device, so the classification arrives as an observation beside the frame
instead of via the cloud chain. Model dir (UII_BFP_MODEL_DIR, default
/etc/eaos/models/bfp-dose): model.onnx + meta.json. Degrades to unavailable
without files or onnxruntime. Swap in a newer model (e.g. the OSU benchmark
model) by replacing the dir contents and restarting — the interface is the
meta.json contract, not the architecture.
"""
from __future__ import annotations

import json
import os

import numpy as np

DEFAULT_DIR = "/etc/eaos/models/bfp-dose"


class BfpDoseModel:
    def __init__(self, model_dir: str | None = None):
        self.dir = model_dir or os.environ.get("UII_BFP_MODEL_DIR", DEFAULT_DIR)
        self.meta = None
        self.sess = None
        self.error = None
        try:
            with open(os.path.join(self.dir, "meta.json")) as f:
                self.meta = json.load(f)
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = int(os.environ.get("UII_BFP_THREADS", "3"))
            self.sess = ort.InferenceSession(os.path.join(self.dir, "model.onnx"),
                                             sess_options=so, providers=["CPUExecutionProvider"])
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"

    @property
    def available(self) -> bool:
        return self.sess is not None

    @property
    def model_id(self) -> str:
        return (self.meta or {}).get("model_id", "bfp-dose/unknown")

    def predict(self, rgb: np.ndarray) -> dict | None:
        if not self.available:
            return None
        from PIL import Image
        size = int(self.meta["input"]["size"])
        img = Image.fromarray(np.ascontiguousarray(rgb[..., :3]).astype(np.uint8)).resize((size, size), Image.BILINEAR)
        a = np.asarray(img).astype(np.float32) / 255.0
        a = (a - np.array(self.meta["input"]["mean"], np.float32)) / np.array(self.meta["input"]["std"], np.float32)
        logits = self.sess.run(None, {"image": a.transpose(2, 0, 1)[None]})[0][0]
        classes = self.meta["classes"]
        head = (self.meta.get("head") or {"type": "softmax"})
        if head.get("type") == "corn":
            # CORN ordinal decode (OSU spec §5.2): logits are P(rank>0), P(rank>1|rank>0);
            # cumprod enforces monotonicity; rank order comes from head["rank_to_class"].
            sig = 1.0 / (1.0 + np.exp(-logits))
            cum = np.cumprod(sig)
            rank = np.clip([1.0 - cum[0], cum[0] - cum[1], cum[1]], 0.0, None)
            rank = rank / max(rank.sum(), 1e-8)
            p = np.zeros(len(classes))
            for r, cls in enumerate(head["rank_to_class"]):
                p[classes.index(cls)] = rank[r]
        else:
            p = np.exp(logits - logits.max()); p /= p.sum()
        i = int(p.argmax())
        return {"dose_class": classes[i], "confidence": round(float(p[i]), 4),
                "probs": {c: round(float(x), 4) for c, x in zip(classes, p)}}


class BfpDoseEnsemble:
    """Every model dir under the root runs on each frame — the side-by-side.
    Root layout: <root>/<slot>/model.onnx+meta.json (slot = e.g. eaos-effnet,
    osu-convnext). A bare root with model.onnx directly in it is one model
    (back-compat with the first deploy)."""

    def __init__(self, root: str | None = None):
        self.root = root or os.environ.get("UII_BFP_MODEL_DIR", DEFAULT_DIR)
        self.models: dict[str, BfpDoseModel] = {}
        if os.path.exists(os.path.join(self.root, "model.onnx")):
            self.models["default"] = BfpDoseModel(self.root)
        elif os.path.isdir(self.root):
            for slot in sorted(os.listdir(self.root)):
                d = os.path.join(self.root, slot)
                if os.path.exists(os.path.join(d, "model.onnx")):
                    self.models[slot] = BfpDoseModel(d)

    @property
    def available(self) -> bool:
        return any(m.available for m in self.models.values())

    @property
    def error(self) -> str | None:
        errs = {k: m.error for k, m in self.models.items() if m.error}
        return str(errs) if errs else ("no model dirs found" if not self.models else None)

    @property
    def model_id(self) -> str:
        return "+".join(m.model_id for m in self.models.values() if m.available) or "bfp-dose/none"

    def predict(self, rgb: np.ndarray) -> dict | None:
        out = {k: m.predict(rgb) for k, m in self.models.items() if m.available}
        out = {k: v for k, v in out.items() if v}
        if not out:
            return None
        if len(out) == 1:
            (only,) = out.values()
            return only
        # primary = first slot alphabetically unless meta marks primary: true
        primary = next((k for k, m in self.models.items()
                        if (m.meta or {}).get("primary")), sorted(out)[0])
        agree = len({v["dose_class"] for v in out.values()}) == 1
        return {**out[primary], "primary": primary, "agreement": agree,
                "models": {k: {"model_id": self.models[k].model_id, **v} for k, v in out.items()}}
