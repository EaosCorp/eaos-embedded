"""Hub entrypoint and wiring.

  UII_CONFIG      hub config JSON      (default ./config/hub.json if present)
  UII_HUB_ID      hub identity         (overrides config)
  UII_DATA        data dir             (default ./data)
  UII_SB_PORT     southbound TCP port  (default 7300; 0 = ephemeral)
  UII_API_PORT    HTTP API port        (default 8400; 0 = ephemeral)
  UII_ALLOWED     comma list of adoptable module types (overrides config)
  UII_EXTENSIONS  comma list of extensions (overrides config)
  UII_SPEED       time compression for bench runs (default 1 — REAL TIME)

Run: python3 -m uii.hub.main

Extension loading: each name in config `extensions` resolves to the
piece `uii_<name>` (analyzer, vision) or the hub extension `extensions.<name>`
(authority, detections, exports, faceplate, scheduler), whose `setup(hub)` wires it in through the
hub's hooks BEFORE the API starts serving:

  hub.southbound.interpreters[cls]   result interpreter per instrument class
  hub.gateway.policy                 command authority (decide/defer)
  hub.api_get_routes / api_post_routes   extra /v1 endpoints
  hub.api_auth                       request authentication
  hub.module_row_enrichers           extra columns on /v1/modules
  hub.add_service(thread)            background engines (started/stopped
                                     with the hub; thread has .stop Event)
  hub.store.subscribe()              the evidence fan-out (how engines watch)
"""
from __future__ import annotations

import asyncio
import importlib
import os
import threading
from typing import Optional

from .api import serve_api
from .commands import CommandGateway
from .config import HubConfig
from .evidence import EvidenceStore
from .interpret import make_analyzer_interpreter
from .southbound import SouthboundHub


class Hub:
    """Everything wired together; embeddable (tests, demos) or run as main."""

    def __init__(self, config: Optional[HubConfig] = None,
                 sb_port: int = 7300, api_port: int = 8400,
                 speed: float = 1.0):
        self.config = config or HubConfig(os.environ.get("UII_CONFIG"))
        self.speed = speed
        os.makedirs(self.config.data_dir, exist_ok=True)
        self.store = EvidenceStore(
            os.path.join(self.config.data_dir, "evidence.db"), self.config.hub_id)
        self.southbound = SouthboundHub(self.store, self.config)
        self.loop = asyncio.new_event_loop()
        self.gateway = CommandGateway(self.store, self.southbound, self.loop)

        # -- extension hooks (populated by setup(hub) calls) -----------------
        self.api_get_routes: list = []
        self.api_post_routes: list = []
        self.api_auth = None
        self.module_row_enrichers: list = []
        self.services: list = []

        # core registers the reference chemical-analyzer interpreter;
        # the analyzer extension replaces it with the full field version
        self.southbound.interpreters["chemical-analyzer"] = make_analyzer_interpreter()

        self._sb_port_req = sb_port
        self._api_port_req = api_port
        self.api_server = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def add_service(self, thread) -> None:
        """Register a background engine (daemon Thread with a .stop Event)."""
        self.services.append(thread)

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        for name in self.config.extensions:
            # instrument PIECES are top-level packages (uii_analyzer, uii_vision);
            # hub extensions (authority, detections, exports, faceplate, scheduler)
            # live under extensions/. Config names them the same way either way.
            try:
                mod = importlib.import_module(f"uii_{name}")
            except ModuleNotFoundError:
                mod = importlib.import_module(f"extensions.{name}")
            mod.setup(self)

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=10)
        self.api_server = serve_api(
            self,
            os.environ.get("UII_API_BIND", os.environ.get("UII_BIND", "0.0.0.0")),
            self._api_port_req)
        self.api_port = self.api_server.server_address[1]
        for svc in self.services:
            svc.start()
        self.store.append("event",
                          {"event": "hub-started", "api_port": self.api_port,
                           "southbound_port": self.sb_port,
                           "extensions": self.config.extensions},
                          "urn:uii:schema:event:0.1", actor="system:hub")
        return self

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        ready = asyncio.Event()

        async def _await_ready():
            await ready.wait()
            self.sb_port = self.southbound.port
            self._ready.set()

        self.loop.create_task(_await_ready())
        try:
            self.loop.run_until_complete(
                self.southbound.serve(
                    os.environ.get("UII_SB_BIND", os.environ.get("UII_BIND", "0.0.0.0")),
                    self._sb_port_req, ready))
        except (KeyboardInterrupt, asyncio.CancelledError, RuntimeError):
            pass  # RuntimeError: loop stopped by Hub.stop() mid-serve
        finally:
            try:
                pending = asyncio.all_tasks(self.loop)
                for t in pending:
                    t.cancel()
                self.loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.loop.close()

    def stop(self):
        """Safe to call more than once (a restart test's finally block
        must never mask the real failure)."""
        self.southbound.draining = True
        for svc in self.services:
            svc.stop.set()
        if self.api_server:
            self.api_server.shutdown()       # stop serving
            self.api_server.server_close()   # and release the listening socket
            self.api_server = None
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError:
            pass  # loop already closed
        if self._thread:
            self._thread.join(timeout=5)


def main():
    cfg_path = os.environ.get("UII_CONFIG")
    if not cfg_path and os.path.exists("./config/hub.json"):
        cfg_path = "./config/hub.json"
    config = HubConfig(cfg_path)
    hub = Hub(config,
              sb_port=int(os.environ.get("UII_SB_PORT", 7300)),
              api_port=int(os.environ.get("UII_API_PORT", 8400)),
              speed=float(os.environ.get("UII_SPEED", 1)))
    hub.start()
    print(f"[hub] {config.hub_id} · api :{hub.api_port} · southbound :{hub.sb_port} "
          f"· data {config.data_dir} · roles {list(config.roles)} "
          f"· extensions {config.extensions or '[]'}")
    try:
        hub._thread.join()
    except KeyboardInterrupt:
        hub.stop()


if __name__ == "__main__":
    main()
