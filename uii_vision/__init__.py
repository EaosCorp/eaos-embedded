"""The vision instrument-class profile (uii-camera-sensor).

Foam detection first, classical CV, multi-observable by design. Loaded like
any UII extension: add "vision" to `extensions` in hub.json and this
`setup(hub)` wires it in through the declared hooks before the API serves.

What it registers:
  * interpreters["vision"]      — grab-on-demand + the re_baseline/set_region/
                                  ptz lifecycle (fires on a capture RESULT)
  * FoamService (add_service)   — the telemetry stream: streamed frames ->
                                  foam_coverage
  * module_row_enrichers        — current foam % per camera on /v1/modules

The module agent is `uii_vision.campod` (run on the edge box). Design
rationale and the full profile: docs/vision-profile.md.
"""
from __future__ import annotations

import os

from .blobstore import FrameBlobStore
from .framesource import recompress
from .interpreter import VisionState, make_vision_interpreter
from .service import FoamService


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _serve_jpeg(handler, data, q, *, immutable, digest=None, frame_time=None):
    """Serve a stored frame small by default (cellular). ?full=1 for original,
    ?w=<px> to cap width, ?q=<1-95> for quality.

    immutable=True only for content-addressed URLs (/v1/frames/<sha256>); the
    per-module "latest" URL must be no-store or a browser tab keeps showing
    the same frame forever. X-Frame-Hash / X-Frame-Time tell the caller which
    frame it actually got (also on HEAD)."""
    full = q.get("full") in ("1", "true", "yes")
    out = data if full else recompress(data, max_w=_int(q.get("w"), 1280),
                                       quality=_int(q.get("q"), 70))
    handler.send_response(200)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(out)))
    handler.send_header("Cache-Control",
                        "public, max-age=31536000, immutable" if immutable
                        else "no-store")
    if digest:
        handler.send_header("X-Frame-Hash", digest)
    if frame_time:
        handler.send_header("X-Frame-Time", frame_time)
    handler.end_headers()
    handler.wfile.write(out)


def _frame_routes(store, blobs):
    def by_hash(handler, m, q):
        data = blobs.get(m.group(1))
        if not data:
            return handler._problem(404, "urn:uii:problem:not-found", "no such frame")
        _serve_jpeg(handler, data, q, immutable=True, digest=m.group(1))

    def latest(handler, m, q):
        obs = store.latest_observation(m.group(1), "frames")
        digest = ((obs or {}).get("data") or {}).get("frame_hash")
        data = blobs.get(digest) if digest else None
        if not data:
            return handler._problem(404, "urn:uii:problem:not-found", "no frame for module")
        _serve_jpeg(handler, data, q, immutable=False, digest=digest,
                    frame_time=(obs or {}).get("time"))

    return by_hash, latest


def _foam_enricher(store):
    def enrich(session):
        obs = store.latest_observation(session.module_id, "foam_coverage")
        iq = store.latest_observation(session.module_id, "image_quality")
        row = {}
        if obs:
            d = obs.get("data") or {}
            row["foam_coverage_pct"] = d.get("value")
            row["foam_type"] = d.get("foam_type")
            row["foam_permitted_use"] = (obs.get("quality") or {}).get("permitted_use")
        if iq:
            row["image_quality"] = (iq.get("data") or {}).get("value")
        return row
    return enrich


def setup(hub):
    frames_dir = os.environ.get("UII_FRAMES_DIR") or os.path.join(
        hub.config.data_dir, "frames")
    blobs = FrameBlobStore(frames_dir)
    state = VisionState()

    hub.southbound.interpreters["vision"] = make_vision_interpreter(blobs, state)
    hub.add_service(FoamService(hub.store, hub.southbound, blobs, state))
    hub.module_row_enrichers.append(_foam_enricher(hub.store))

    # image fetch — compressed by default (cellular). Full frame by hash, or the
    # latest frame per module. GET /v1/frames/<sha256> · /v1/modules/<id>/frame
    by_hash, latest = _frame_routes(hub.store, blobs)
    hub.api_get_routes.append((r"/v1/frames/([0-9a-f]{64})", by_hash))
    hub.api_get_routes.append((r"/v1/modules/([\w-]+)/frame", latest))

    # bearer-token API auth, if a tokens file is present (edge deployment).
    # Absent => open mode (localhost dev / tests). See auth.py.
    from .auth import load_tokens, make_token_auth
    tokens = load_tokens(os.environ.get("UII_API_TOKENS",
                                        "/etc/eaos/uii-api-tokens.json"))
    if tokens:
        hub.api_auth = make_token_auth(tokens)
        print(f"[vision] API token auth ON ({len(tokens)} token(s))", flush=True)

    # expose the shared handles for tests / other extensions
    hub.vision = {"blobs": blobs, "state": state, "frames_dir": frames_dir}
