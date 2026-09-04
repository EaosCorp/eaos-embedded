"""HTTP API + SSE event stream. Stdlib only — the /v1 surface of the core:

  GET  /.well-known/uii                           discovery (no auth, spec 5.1)
  GET  /v1/system · /v1/modules · /v1/roles · /v1/capabilities
  GET  /v1/modules/{id}/commands                  the exposed command surface:
                                                  params, risk, preconditions
  GET  /v1/modules/{id}/history                   the digital record: swaps,
                                                  calibrations, service events
  GET  /v1/observations[?channel=&module=&since=|&last=N] · /v1/observations/latest
  GET  /v1/calibrations[?module=]
  GET  /v1/evidence [·/{id} ·/{id}/lineage]       one query surface
  GET  /v1/events                                 SSE, Last-Event-ID resume
  POST /v1/commands · GET /v1/commands/{id}
  POST /v1/commands/{id}/cancel                   cooperative cancel
  POST /v1/modules/{id}/release                   quarantine release

Extension hooks:
  hub.api_get_routes / hub.api_post_routes — lists of (regex, fn);
    GET fn(handler, match, query) · POST fn(handler, match, body).
    Handlers use self._json / self._problem / self._read_json.
  hub.api_auth — optional fn(handler) -> (actor, ok). When set (the
    authority extension's locked mode), it runs before every request and
    its actor overrides anything a request body claims.
"""
from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..protocol import now_iso

START = time.time()


def make_handler(hub):
    store, southbound, gateway = hub.store, hub.southbound, hub.gateway

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # -- helpers (extension handlers use these too) -------------------

        def _json(self, obj, code=200):
            body = json.dumps(obj, indent=2).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _problem(self, code, ptype, detail):
            body = json.dumps({"type": ptype, "status": code, "detail": detail}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/problem+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                return json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return None

        def _auth(self):
            if hub.api_auth is None:
                return None, True
            return hub.api_auth(self)

        # -- GET ------------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path

            if path == "/.well-known/uii":     # discovery is auth-free
                return self._json({
                    "uii": "0.2", "hub": store.hub_id,
                    "api": "/v1", "events": {"sse": "/v1/events"},
                    "auth": ({"modes": ["bearer"]} if hub.api_auth
                             else {"modes": ["open"]}),
                    "vendor": {"name": "Eaos", "product": "uii-analyzer"}})

            actor, ok = self._auth()
            if not ok:
                return

            if path == "/v1/system":
                return self._json({
                    "hub": store.hub_id, "uii": "0.2",
                    "auth": "token" if hub.api_auth else "open",
                    "extensions": hub.config.extensions,
                    "time": now_iso(), "uptime_s": int(time.time() - START),
                    "modules_operational": len(southbound.sessions),
                    "modules_quarantined": len(southbound.quarantined)})

            if path == "/v1/modules":
                return self._json({"items": self._module_rows()})

            if path == "/v1/roles":
                occupancy = {s.role: s.module_id
                             for s in southbound.sessions.values() if s.role}
                return self._json({"items": [
                    {"slot": slot, **cfg,
                     "occupied_by": occupancy.get(cfg["role"])}
                    for slot, cfg in southbound.config.roles.items()]})

            if path == "/v1/capabilities":
                return self._json({
                    "hub": {"id": store.hub_id},
                    "modules": [{"id": s.module_id, "type": s.module_type,
                                 "role": s.role, "manifest": s.manifest}
                                for s in southbound.sessions.values()]})

            if path == "/v1/observations":
                # ?last=N returns the N most recent matches (ascending order,
                # trend-ready); otherwise a forward cursor walk from ?since=
                if q.get("last"):
                    envs = store.query(kind="observation",
                                       module=q.get("module"),
                                       channel=q.get("channel"),
                                       descending=True,
                                       limit=int(q["last"]))
                    envs.reverse()
                else:
                    envs = store.query(kind="observation",
                                       module=q.get("module"),
                                       channel=q.get("channel"),
                                       since_seq=int(q.get("since", 0)),
                                       limit=int(q.get("limit", 200)))
                return self._json({"items": envs,
                                   "next_cursor": envs[-1]["sequence"] if envs else None})

            m = re.fullmatch(r"/v1/modules/([\w-]+)/commands", path)
            if m:
                session = southbound.sessions.get(m.group(1))
                if not session:
                    return self._problem(404, "urn:uii:problem:not-found",
                                         f"no adopted module '{m.group(1)}'")
                return self._json({"module": m.group(1),
                                   "commands": session.manifest.get("commands", [])})

            m = re.fullmatch(r"/v1/modules/([\w-]+)/history", path)
            if m:
                envs = store.query(module=m.group(1),
                                   kind=q.get("kind", "identity,calibration,config,event"),
                                   since_seq=int(q.get("since", 0)),
                                   limit=int(q.get("limit", 500)))
                return self._json({"module": m.group(1), "items": envs})

            if path == "/v1/observations/latest":
                out = []
                for s in southbound.sessions.values():
                    for ch in [c["name"] for c in s.manifest.get("channels", [])]:
                        env = store.latest_observation(s.module_id, ch)
                        if env:
                            out.append(env)
                return self._json({"items": out})

            if path == "/v1/calibrations":
                mods = ([q["module"]] if q.get("module")
                        else list(southbound.sessions.keys()))
                items = [c for c in (store.latest_calibration(m) for m in mods) if c]
                return self._json({"items": items})

            if path == "/v1/evidence":
                envs = store.query(kind=q.get("kind"), module=q.get("module"),
                                   since_seq=int(q.get("since", 0)),
                                   limit=int(q.get("limit", 100)))
                return self._json({"items": envs,
                                   "next_cursor": envs[-1]["sequence"] if envs else None})

            m = re.fullmatch(r"/v1/evidence/([\w-]+)/lineage", path)
            if m:
                lin = store.lineage(m.group(1))
                return self._json(lin) if lin else self._problem(
                    404, "urn:uii:problem:not-found", "no such envelope")

            m = re.fullmatch(r"/v1/evidence/([\w-]+)", path)
            if m:
                env = store.get(m.group(1))
                return self._json(env) if env else self._problem(
                    404, "urn:uii:problem:not-found", "no such envelope")

            m = re.fullmatch(r"/v1/commands/([\w-]+)", path)
            if m:
                st = gateway.status(m.group(1))
                return self._json(st) if st else self._problem(
                    404, "urn:uii:problem:not-found", "no such command")

            if path == "/v1/events":
                return self._sse(q)

            for pattern, fn in hub.api_get_routes:      # extension routes
                m = re.fullmatch(pattern, path)
                if m:
                    return fn(self, m, q)

            return self._problem(404, "urn:uii:problem:not-found", path)

        # -- HEAD -------------------------------------------------------------

        def do_HEAD(self):
            """Same status + headers as GET, no body. Without this the stdlib
            answers 501 with an HTML body, which confuses `curl -I`, uptime
            probes and browsers pre-flighting an <img>. Runs do_GET with a
            writer that forwards the header block and swallows the body."""
            if urlparse(self.path).path == "/v1/events":       # SSE never ends
                return self._problem(405, "urn:uii:problem:method-not-allowed",
                                     "HEAD not supported on the event stream")
            real, handler = self.wfile, self

            class HeadersOnly:
                body = False

                def write(self, b):
                    if not self.body:
                        real.write(b)
                    return len(b)

                def flush(self):
                    real.flush()

            w = HeadersOnly()
            end_headers = self.end_headers

            def end_headers_then_mute():
                end_headers()               # flushes the header block via w
                w.body = True

            self.wfile, self.end_headers = w, end_headers_then_mute
            try:
                self.do_GET()
            finally:
                self.wfile = real
                del self.end_headers

        # -- POST -------------------------------------------------------------

        def do_POST(self):
            auth_actor, ok = self._auth()
            if not ok:
                return
            url = urlparse(self.path)

            m = re.fullmatch(r"/v1/commands/([\w-]+)/cancel", url.path)
            if m:
                summary, problem = gateway.cancel(
                    m.group(1), actor=auth_actor or "user:api")
                if problem:
                    code = 404 if problem["type"].endswith("not-found") else 422
                    return self._problem(code, problem["type"], problem["detail"])
                return self._json(summary, 202)

            m = re.fullmatch(r"/v1/modules/([\w-]+)/release", url.path)
            if m:
                session = southbound.quarantined.get(m.group(1))
                if not session:
                    return self._problem(404, "urn:uii:problem:not-found",
                                         f"no quarantined module '{m.group(1)}'")
                fut = asyncio.run_coroutine_threadsafe(
                    session.release(actor=auth_actor or "user:api"), hub.loop)
                fut.result(timeout=5)
                return self._json({"released": m.group(1),
                                   "serial_trusted": session.serial})

            if url.path == "/v1/commands":
                body = self._read_json()
                if body is None:
                    return self._problem(400, "urn:uii:problem:validation",
                                         "invalid JSON")
                module = body.get("module") or (body.get("target") or {}).get("module")
                if not module or not body.get("type"):
                    return self._problem(400, "urn:uii:problem:validation",
                                         "need module and type")
                # identity comes from the credential when auth is on; the
                # on-hub API is the "local" ingress path — future OT/cloud/
                # cellular listeners stamp their own
                env, problem = gateway.submit(
                    module, body["type"], body.get("params"),
                    actor=auth_actor or body.get("actor", "user:local"),
                    ingress="local",
                    idempotency_key=self.headers.get("Idempotency-Key"))
                if problem:
                    return self._problem(422, problem["type"], problem["detail"])
                resp = {"command_id": env["data"]["command_id"],
                        "evidence_id": env["id"]}
                if env["data"].get("approval_id"):
                    resp["approval_required"] = True
                    resp["approval_id"] = env["data"]["approval_id"]
                return self._json(resp, 202)

            body = None
            for pattern, fn in hub.api_post_routes:     # extension routes
                m = re.fullmatch(pattern, url.path)
                if m:
                    if body is None:
                        body = self._read_json()
                        if body is None:
                            return self._problem(400, "urn:uii:problem:validation",
                                                 "invalid JSON")
                    body.setdefault("_actor", auth_actor)
                    return fn(self, m, body)

            return self._problem(404, "urn:uii:problem:not-found", url.path)

        # -- shared views ---------------------------------------------------------

        def _module_rows(self):
            live = {}
            for s in list(southbound.sessions.values()) + list(southbound.quarantined.values()):
                row = {"id": s.module_id, "type": s.module_type,
                       "serial": s.serial, "fw": s.fw, "slot": s.slot,
                       "state": s.state, "role": s.role, "mode": s.mode,
                       "module_state": s.module_state,
                       "last_seen_s_ago": round(time.time() - s.last_seen, 1)}
                for enrich in hub.module_row_enrichers:   # e.g. NE107 status
                    row.update(enrich(s) or {})
                live[s.module_id] = row
            rows = list(live.values())
            for mid, info in southbound.registry.items():
                if mid not in live:
                    rows.append(info)
            return rows

        # -- SSE -----------------------------------------------------------

        def _sse(self, q):
            since = int(q.get("since", self.headers.get("Last-Event-ID", 0) or 0))
            kinds = set(q["kind"].split(",")) if q.get("kind") else None
            sub = store.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                for env in store.query(since_seq=since, limit=500):
                    if not kinds or env["kind"] in kinds:
                        self._sse_write(env)
                while True:
                    try:
                        env = sub.get(timeout=15)
                        if not kinds or env["kind"] in kinds:
                            self._sse_write(env)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                store.unsubscribe(sub)

        def _sse_write(self, env):
            frame = (f"id: {env['sequence']}\nevent: {env['kind']}\n"
                     f"data: {json.dumps(env)}\n\n").encode()
            self.wfile.write(frame)
            self.wfile.flush()

    return Handler


def serve_api(hub, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(hub))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
