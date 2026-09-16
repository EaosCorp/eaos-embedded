"""Edge launcher — one hub + one campod per Jetson, real camera, runs forever.

This is what the systemd unit (uii-vision.service) runs on an edge box: an
embedded hub with the vision extension, and campod pulling real frames from
the Reolink, co-located in one process (one hub per Jetson). Camera
credentials come from /etc/eaos/camera-credentials.env.

Config via env (all optional):
  UII_DATA           hub data dir           (default /var/lib/eaos/uii-data)
  UII_FRAMES_DIR     shared frame blobs     (default /var/lib/eaos/frames)
  UII_API_PORT       hub HTTP API           (default 8400)   UII_SB_PORT 7300
  UII_MODULE_ID campod-01   UII_SLOT slot-1   UII_CADENCE_S 15   UII_HUB_ID hub-edge-01

Pushing to a work plane (the `publish` extension; all optional, and absent = off):
  UII_PUBLISH_URL           https://<plane-host>/api/frames/ingest   -- setting it turns it on
  UII_PUBLISH_EVERY_S       900        match the plant's cameras.yaml `every`
  UII_PUBLISH_CAMERA        the plane's camera id, if it differs from UII_MODULE_ID
  UII_PUBLISH_TOKEN_FILE    /etc/eaos/frames-ingest-token
  UII_PUBLISH_MAX_W 1280    UII_PUBLISH_QUALITY 70   the cellular squeeze

Run: python3 -m uii_vision.edge
"""
from __future__ import annotations

import asyncio
import json
import os

from uii.hub.config import HubConfig
from uii.hub.main import Hub

from .campod import CamModule

CREDS = "/etc/eaos/camera-credentials.env"


def load_creds(path=CREDS):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k, v)


def main():
    load_creds()
    # Module traffic (southbound) stays on loopback — campod is on this box.
    # The API faces the tailnet by default so you can fetch frames from your
    # laptop; it is protected by the bearer-token gate (UII_API_TOKENS). Set
    # UII_API_BIND=127.0.0.1 to keep it local-only.
    os.environ.setdefault("UII_SB_BIND", "127.0.0.1")
    os.environ.setdefault("UII_API_BIND", "0.0.0.0")
    data_dir = os.environ.get("UII_DATA", "/var/lib/eaos/uii-data")
    frames_dir = os.environ.setdefault("UII_FRAMES_DIR", "/var/lib/eaos/frames")
    module_id = os.environ.get("UII_MODULE_ID", "campod-01")
    slot = os.environ.get("UII_SLOT", "slot-1")
    cadence = os.environ.get("UII_CADENCE_S", "15")
    os.makedirs(data_dir, exist_ok=True)

    role = {"role": os.environ.get("UII_ROLE", "aeration-foam-cam"),
            "cadence_s": float(cadence), "control_ok": False}
    # Site ROI: the commissioned outlines of the basins/channels this camera
    # watches, as fractional polygons ({"polygons": {name: [[x, y], ...]}}).
    # Lives next to the camera credentials, never in the image; absent = whole
    # frame. Canonical copies: uii_vision/config/roi/<hostname>.json.
    roi_path = os.environ.get("UII_ROI_FILE", "/etc/eaos/vision-roi.json")
    if os.path.exists(roi_path):
        with open(roi_path) as f:
            role["roi"] = json.load(f)
        n = len((role["roi"].get("polygons") or {}))
        print(f"[uii-vision] roi {roi_path}: {n} polygon(s)", flush=True)
    # Pushing to a work plane: the plane cannot reach a tailnet, so the box sends outward on a
    # cadence. No URL means the extension is not even loaded, which is how every box that has no
    # plane behaves today.
    publish = {}
    plane_url = os.environ.get("UII_PUBLISH_URL", "").strip()
    if plane_url:
        publish = {"url": plane_url,
                   "every_s": float(os.environ.get("UII_PUBLISH_EVERY_S", "900")),
                   "token_file": os.environ.get("UII_PUBLISH_TOKEN_FILE", "/etc/eaos/frames-ingest-token"),
                   "max_w": int(os.environ.get("UII_PUBLISH_MAX_W", "1280")),
                   "quality": int(os.environ.get("UII_PUBLISH_QUALITY", "70")),
                   "modules": {module_id: os.environ.get("UII_PUBLISH_CAMERA", module_id)}}
        print(f"[uii-vision] publish {module_id} → {plane_url} every {publish['every_s']:.0f}s", flush=True)
    cfg = {"hub_id": os.environ.get("UII_HUB_ID", "hub-edge-01"),
           "allowed_types": ["vision-cam"],
           "extensions": ["vision"] + (["publish"] if publish else []),
           "data_dir": data_dir, "roles": {slot: role},
           **({"publish": publish} if publish else {})}
    cfgp = os.path.join(data_dir, "hub.json")
    with open(cfgp, "w") as f:
        json.dump(cfg, f)

    hub = Hub(HubConfig(cfgp),
              sb_port=int(os.environ.get("UII_SB_PORT", "7300")),
              api_port=int(os.environ.get("UII_API_PORT", "8400")),
              speed=1.0).start()
    print(f"[uii-vision] hub up · api :{hub.api_port} · sb :{hub.sb_port} · "
          f"frames {frames_dir}", flush=True)

    env = dict(os.environ, UII_HUB=f"127.0.0.1:{hub.sb_port}",
               UII_MODULE_ID=module_id, UII_SLOT=slot, UII_TYPE="vision-cam",
               UII_SERIAL=os.environ.get("UII_SERIAL", f"SN-{module_id.upper()}"),
               UII_CADENCE_S=cadence, UII_FRAMES_DIR=frames_dir)
    env.pop("UII_SIM", None)                 # real camera
    mod = CamModule(env)

    async def _run():
        asyncio.get_event_loop().create_task(mod.health_loop())
        asyncio.get_event_loop().create_task(mod.cadence_loop())
        await mod.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
