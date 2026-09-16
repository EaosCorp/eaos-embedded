"""publish extension — send each camera's newest frame and its readings to a work plane.

The plane cannot come and get them. The boxes sit on the plant's tailnet, on cellular, and a
cloud work plane has no route to a 100.x address; giving it one would put a cloud workload inside
the plant's private network and would multiply across every plant. So the hub pushes outward, on
a cadence, and every reader on the plane is served from what arrived.

Reads in process -- the evidence store and the frame blobs -- so no viewer token and no loopback
HTTP. What it sends is what `/v1/modules/<id>/frame` and `/v1/modules` would have answered.

Enable: "extensions": ["vision", "publish"] in hub.json, with

    "publish": {
      "url": "https://<plane-host>/api/frames/ingest",
      "every_s": 900,                     // match the plant's cameras.yaml `every`
      "token_file": "/etc/eaos/frames-ingest-token",
      "max_w": 1280, "quality": 70,       // the cellular squeeze, as the API route uses
      "modules": {"campod-01": "campod-01"}   // box module id -> the plane's camera id;
                                              // omit to publish every module that has a frame
    }

The token is the plane's INGEST token: it opens the mailbox and nothing else, so a box in a basin
never holds the credential that opens the plane. It is read from a file outside the repo.

Hooks used: `hub.add_service` (a background thread), `hub.store`, and the vision piece's frame
blobs. Declaring the extension without a `publish.url` is a no-op, so the same hub.json ships to a
plant that has no plane yet.
"""
from .publish import Publisher


def setup(hub):
    cfg = dict((getattr(hub.config, "raw", None) or {}).get("publish") or {})
    if not cfg.get("url"):
        return                      # declared but not configured: nothing to send anywhere
    hub.add_service(Publisher(hub, cfg))
