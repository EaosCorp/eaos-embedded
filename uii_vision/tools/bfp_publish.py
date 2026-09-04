#!/usr/bin/env python3
"""Publish BFP dose results into the DC Water CV metadata universe — on a budget.

Runs ON THE PI (uii-bfp-publish.timer, hourly). Bridges the new on-device
pipeline (uii-vision frames + two-model `bfp_dose` observations in evidence.db)
into the existing Golden Schema (Cosmos `CvMetadataDB/images` + blob `images`)
that dcwater.eaos.ai reads — so the flywheel keeps turning with no cloud
inference call, and only frames worth an expert's time cross the cellular link.

Selection per local day (mirrors the basin-camera digest policy):
  * review frames  — the two models DISAGREE, or the primary's confidence is
                     below LOW_CONF: status `needs_expert_review`. Capped.
  * keyframes      — first frame of every KEY_EVERY_H hours: status
                     `auto_accepted` when the models agree confidently.
Every published doc gets its image uploaded (the review app requires a
blob_url), recompressed to IMG_W px (~200-250 KB). Frames not selected are
NOT published — their predictions stay in the on-device evidence record and
the daily digest. Idempotent: state in /var/lib/eaos/bfp-publish/state.json,
blob-exists check and Cosmos conflict handling from the old uploader.

Budget at defaults (KEY_EVERY_H=2, MAX_REVIEW=10, IMG_W=1280 q75): worst case
~22 images/day ≈ 5 MB ≈ $1.05/day — vs ~$4.5/day for the old
upload-everything chain.

Credentials: /home/dcwater-1/.gopro.env (AZURE_STORAGE_CONNECTION_STRING,
AZURE_COSMOS_ENDPOINT, AZURE_COSMOS_KEY) — same file the legacy chain uses.

    /opt/eaos/venv/bin/python -m extensions.vision.tools.bfp_publish [--dry]
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB = "/var/lib/eaos/uii-data/evidence.db"
FRAMES = "/var/lib/eaos/frames"
STATE_DIR = "/var/lib/eaos/bfp-publish"
CREDS = os.environ.get("BFP_PUB_CREDS", "/home/dcwater-1/.gopro.env")

E = os.environ.get
EQUIPMENT = E("BFP_EQUIPMENT_ID", "A4")
KEY_EVERY_H = int(E("BFP_PUB_KEY_EVERY_H", "2"))
MAX_REVIEW = int(E("BFP_PUB_MAX_REVIEW", "10"))
LOW_CONF = float(E("BFP_PUB_LOW_CONF", "0.70"))
AUTO_CONF = float(E("BFP_PUB_AUTO_CONF", "0.95"))
IMG_W, IMG_Q = int(E("BFP_PUB_IMG_W", "1280")), int(E("BFP_PUB_IMG_Q", "75"))
TZ_OFFSET_H = int(E("UII_TZ_OFFSET_H", "-4"))
CAMERA_MODEL = E("CAMERA_MODEL", "GoPro Hero 11")


def load_env(path):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def image_id_for(ts: datetime) -> str:
    # legacy convention: A4_YY-MM-DD-HH-MM-SS.jpg -> doc id with dots as underscores
    return f"{EQUIPMENT}_{ts.strftime('%y-%m-%d-%H-%M-%S')}_jpg"


def partition_key(image_id, primary_label):
    """shared/utils/partition_logic.get_partition_key, with the on-device
    prediction standing in for label_counts (rare-class = Underdose)."""
    hash_val = int(hashlib.md5(image_id.encode()).hexdigest(), 16) % 100
    if primary_label == "Underdose":
        return "train" if hash_val < 70 else ("validation" if hash_val < 85 else "test")
    return "train" if hash_val < 80 else ("validation" if hash_val < 90 else "test")


def new_observations(state):
    c = sqlite3.connect(DB)
    rows = []
    for seq, t, body in c.execute(
            "select seq, time, body from envelopes where kind='observation' and channel='bfp_dose' "
            "and seq > ? order by seq", (state.get("last_seq", 0),)):
        d = json.loads(body); data = d.get("data") or {}
        if data.get("frame_hash"):
            rows.append({"seq": seq, "time": t, "data": data})
    return rows


def select(rows, state):
    tz = timezone(timedelta(hours=TZ_OFFSET_H))
    picked = []
    day_counts = state.setdefault("review_counts", {})
    seen_slots = state.setdefault("key_slots", {})
    for r in rows:
        d = r["data"]
        ts = datetime.fromisoformat(r["time"].replace("Z", "+00:00")).astimezone(tz)
        day = ts.strftime("%Y-%m-%d")
        models = d.get("models") or {}
        agree = d.get("agreement", True)
        conf = d.get("confidence", 1.0)
        reason = None
        if models and (not agree or conf < LOW_CONF):
            if day_counts.get(day, 0) < MAX_REVIEW:
                reason = "review"; day_counts[day] = day_counts.get(day, 0) + 1
        slot = f"{day}:{ts.hour // KEY_EVERY_H}"
        if reason is None and slot not in seen_slots:
            seen_slots[slot] = 1; reason = "keyframe"
        if reason:
            picked.append({**r, "reason": reason, "ts": ts})
    # keep state maps bounded
    for m in (day_counts, seen_slots):
        for k in sorted(m)[:-14]:
            m.pop(k, None)
    return picked


def to_doc(item, blob_url, compressed_kb):
    d = item["data"]; models = d.get("models") or {}
    iid = image_id_for(item["ts"].astimezone(timezone.utc))
    primary = d.get("value"); conf = d.get("confidence", 0.0)
    agree = d.get("agreement", True)
    now = datetime.now(timezone.utc).isoformat()
    preds = []
    for slot, m in (models.items() or []):
        preds.append({"model_version": m.get("model_id", slot), "predicted_label": m.get("dose_class"),
                      "confidence": m.get("confidence"), "probabilities": m.get("probs"),
                      "timestamp": now, "inference_location": "edge-device", "slot": slot})
    if not preds:                       # single-model observation
        preds.append({"model_version": d.get("model_id"), "predicted_label": primary,
                      "confidence": conf, "probabilities": d.get("probs"),
                      "timestamp": now, "inference_location": "edge-device"})
    if item["reason"] == "review":
        status = "needs_expert_review"
    else:
        status = "auto_accepted" if (agree and conf >= AUTO_CONF) else "needs_expert_review" \
            if conf < LOW_CONF else "auto_accepted"
    return {
        "id": iid,
        "blob_url": blob_url,
        "blob_urls": {"compressed": blob_url},
        "partition_key": partition_key(iid, primary),
        "status": status,
        "metadata": {"ingested_at": now, "equipment_id": EQUIPMENT, "source_type": "gopro",
                     "image_specs": {"camera_model": CAMERA_MODEL,
                                     "compressed_file_size_kb": compressed_kb,
                                     "compression_timestamp": now},
                     "edge": {"frame_hash": d["frame_hash"], "publish_reason": item["reason"],
                              "models_agree": agree}},
        "operational_context": {"timestamp_aligned": False},
        "flags": [],
        "model_predictions": preds,
        "annotations": [],
        "consensus_metadata": {},
    }


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--dry", action="store_true"); a = ap.parse_args()
    load_env(CREDS)
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = os.path.join(STATE_DIR, "state.json")
    state = json.load(open(state_path)) if os.path.exists(state_path) else {}
    rows = new_observations(state)
    picked = select(rows, state)
    print(f"{len(rows)} new bfp_dose observations; publishing {len(picked)}")
    if a.dry:
        for p in picked:
            print("  ", p["time"], p["reason"], p["data"].get("value"), p["data"].get("confidence"))
        return
    if picked:
        from PIL import Image
        from azure.storage.blob import BlobServiceClient
        from azure.core.exceptions import ResourceNotFoundError
        from azure.cosmos import CosmosClient
        from azure.cosmos.exceptions import CosmosResourceExistsError
        bsc = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
        cos = (CosmosClient(os.environ["AZURE_COSMOS_ENDPOINT"], os.environ["AZURE_COSMOS_KEY"])
               .get_database_client(os.environ.get("COSMOS_DATABASE", "CvMetadataDB"))
               .get_container_client(os.environ.get("COSMOS_CONTAINER", "images")))
        total = 0
        for p in picked:
            h = p["data"]["frame_hash"]
            src = os.path.join(FRAMES, h[:2], h + ".jpg")
            if not os.path.exists(src):
                continue
            ts = p["ts"].astimezone(timezone.utc)
            stem = f"{EQUIPMENT}_{ts.strftime('%y-%m-%d-%H-%M-%S')}"
            blob_path = f"{EQUIPMENT}/{ts.strftime('%Y/%m/%d')}/{stem}_1600.jpg"
            img = Image.open(src).convert("RGB")
            if img.width > IMG_W:
                img = img.resize((IMG_W, round(img.height * IMG_W / img.width)), Image.LANCZOS)
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=IMG_Q, optimize=True)
            data = buf.getvalue(); total += len(data)
            bc = bsc.get_blob_client(container=os.environ.get("BLOB_CONTAINER", "images"), blob=blob_path)
            try:
                bc.get_blob_properties()
            except ResourceNotFoundError:
                bc.upload_blob(data=data, overwrite=True, max_concurrency=1, timeout=120)
            doc = to_doc(p, bc.url, round(len(data) / 1024, 1))
            try:
                cos.create_item(body=doc)
            except CosmosResourceExistsError:
                pass
            state["last_seq"] = p["seq"]
            json.dump(state, open(state_path, "w"))
        if rows:
            state["last_seq"] = max(state.get("last_seq", 0), rows[-1]["seq"])
        json.dump(state, open(state_path, "w"))
        print(f"published {len(picked)} docs, {total / 1e6:.2f} MB uploaded")
    else:
        if rows:
            state["last_seq"] = rows[-1]["seq"]
        json.dump(state, open(state_path, "w"))


if __name__ == "__main__":
    main()
