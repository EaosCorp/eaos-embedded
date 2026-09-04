"""mqtt_shim — presents legacy MQTT NH4MOD units as UII modules.

The Stage-1 shim from tools/legacy-shim.md, revived 2026-08-25 for the
POC era: a unit still running the legacy serial_mqtt_gateway (northbound
MQTT, A. Williamson 2026-03) plugs into the instrument LAN, talks to the
mosquitto broker on the hub box, and this service gives it a UII face —
it dials the hub as the module, announces a manifest, and translates both
directions:

  hub CMD {type, params}       -> cmd/{pi}/action {"action", "request_id", **params}
                                  (legacy takes std_conc at the TOP level)
  tele/{pi}/action_status      -> ACK / PROGRESS / RESULT  (request_id == command_id)
  tele/{pi}/data (adc_capture) -> TELEM observation, channel detector_raw
  tele/{pi}/results            -> TELEM observation, channel <analyte>
                                  (mg/L from the unit's ONBOARD cal math —
                                  declared derived_by_hub: false)
  tele/{pi}/run_summary · raw · port_a/b -> TELEM event
  tele/{pi}/heartbeat          -> TELEM health

A unit appears on the hub when its first MQTT message is seen and says BYE
after offline_after_s of silence, so `uii modules` reflects what is
actually alive. Retire this service unit-by-unit as modules move to native
UII agents (pimod today, the MCU front-end later) — nothing upstream of
the hub changes when that happens.

Config: /etc/uii/mqtt-shim.json (or $UII_SHIM_CONFIG)
Run: python3 -m uii_analyzer.mqtt_shim
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from uii.protocol import PROTO, encode, now_iso

from .mqtt_min import MQTTClient

STATE_FOR_ACTION = {"prime": "priming", "calibrate": "calibrating",
                    "sample": "sampling"}


def load_config() -> dict:
    path = os.environ.get("UII_SHIM_CONFIG", "/etc/uii/mqtt-shim.json")
    with open(path) as f:
        return json.load(f)


def manifest(analyte: str) -> dict:
    a = analyte.lower()
    return {
        "instrument_class": "chemical-analyzer",
        "analyte": analyte,
        "method": {"id": f"{a}-legacy-prod4", "version": "prod4"},
        "via": "mqtt-shim",
        "channels": [
            {"name": a, "unit": "mg/L", "derived_by_hub": False,
             "doc": "computed by the legacy gateway's onboard cal math"},
            {"name": "detector_raw", "unit": "V"},
        ],
        "commands": [
            {"type": "take_control", "risk": "disruptive"},
            {"type": "bridge", "risk": "disruptive"},
            {"type": "prime", "risk": "routine"},
            {"type": "calibrate", "risk": "disruptive",
             "params": {"std_conc": {"type": "number", "required": True,
                                     "max": 1000, "unit": "mg/L",
                                     "doc": "standard concentration; the "
                                            "unit enforces > 0"}}},
            {"type": "sample", "risk": "routine"},
        ],
    }


class ShimUnit:
    """One legacy unit's UII face: a hub connection plus the translation."""

    def __init__(self, pi_id, cfg, mqtt, hub_addr, offline_after_s):
        self.pi_id = pi_id
        self.slot = cfg["slot"]
        self.analyte = cfg.get("analyte", "NH4")
        self.serial = cfg["serial"]
        self.module_type = cfg.get("type", f"nh4mod-{self.analyte.lower()}")
        self.mqtt = mqtt
        self.hub_addr = hub_addr
        self.offline_after_s = offline_after_s
        self.last_seen = 0.0
        self.writer = None
        self.local_seq = 0
        self.pending: dict[str, dict] = {}   # command_id -> {"acked": bool}
        self._task = None

    # -- northbound (to the hub) --------------------------------------------

    def seen(self):
        self.last_seen = time.time()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def _send(self, msg):
        if self.writer:
            try:
                self.writer.write(encode(msg))
                await self.writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                self.writer = None

    def telem(self, kind, data, channel=None, command_id=None):
        self.local_seq += 1
        asyncio.ensure_future(self._send(
            {"t": "TELEM", "kind": kind, "channel": channel,
             "command_id": command_id, "local_seq": self.local_seq,
             "time": now_iso(), "data": data}))

    async def _run(self):
        backoff = 1.0
        while time.time() - self.last_seen < self.offline_after_s:
            try:
                reader, writer = await asyncio.open_connection(*self.hub_addr)
                self.writer = writer
                await self._send({"t": "HELLO", "proto": PROTO,
                                  "module_id": self.pi_id,
                                  "type": self.module_type,
                                  "serial": self.serial,
                                  "fw": "legacy-prod4+shim",
                                  "slot": self.slot,
                                  "manifest": manifest(self.analyte)})
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                resp = json.loads(line.decode())
                if resp.get("t") == "QUARANTINE":
                    print(f"[{self.pi_id}] quarantined: {resp.get('reason')}")
                    while True:   # powered, mute; release closes the socket
                        line = await reader.readline()
                        if not line or json.loads(
                                line.decode()).get("t") == "QUARANTINE_RELEASED":
                            break
                    writer.close()
                    self.writer = None
                    continue
                if resp.get("t") != "HELLO_OK":
                    raise ConnectionError(f"unexpected {resp.get('t')}")
                await self._send({"t": "ROLE_OK", "role": resp.get("role")})
                print(f"[{self.pi_id}] adopted as {resp.get('role')} (via shim)")
                backoff = 1.0
                while True:
                    # silence check every pass — hub PINGs keep lines flowing,
                    # so the read timeout alone can never prove the UNIT is gone
                    if time.time() - self.last_seen >= self.offline_after_s:
                        print(f"[{self.pi_id}] silent on MQTT "
                              f"{self.offline_after_s:.0f}s — BYE")
                        await self._send({"t": "BYE",
                                          "reason": "unit silent on MQTT"})
                        writer.close()
                        self.writer = None
                        return
                    wait = max(1.0, self.offline_after_s
                               - (time.time() - self.last_seen))
                    try:
                        line = await asyncio.wait_for(reader.readline(),
                                                      timeout=wait)
                    except asyncio.TimeoutError:
                        continue
                    if not line:
                        break
                    msg = json.loads(line.decode())
                    if msg.get("t") == "CMD":
                        await self.deliver(msg)
                    elif msg.get("t") == "PING":
                        await self._send({"t": "PONG"})
            except (OSError, ConnectionError, asyncio.TimeoutError,
                    json.JSONDecodeError):
                pass
            self.writer = None
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)

    # -- hub CMD -> legacy MQTT ---------------------------------------------

    async def deliver(self, cmd):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}
        self.pending[cmd_id] = {"acked": False}
        payload = {"action": cmd_type, "request_id": cmd_id, **params}
        if not await self.mqtt.publish(f"cmd/{self.pi_id}/action", payload):
            self.pending.pop(cmd_id, None)
            await self._send({"t": "ACK", "command_id": cmd_id,
                              "accepted": False,
                              "reason": "MQTT broker unreachable"})

    # -- legacy MQTT -> hub ---------------------------------------------------

    def on_tele(self, subtopic: str, payload: bytes):
        self.seen()
        try:
            data = json.loads(payload.decode())
        except (ValueError, UnicodeDecodeError):
            data = {"raw": payload.decode(errors="replace")}
        if not isinstance(data, dict):
            data = {"value": data}
        if subtopic == "action_status":
            asyncio.ensure_future(self._action_status(data))
        elif subtopic == "data":
            self.telem("observation", data, channel="detector_raw",
                       command_id=data.get("request_id"))
        elif subtopic == "results":
            self.telem("observation", data, channel=self.analyte.lower(),
                       command_id=data.get("request_id"))
        elif subtopic == "heartbeat":
            self.telem("health", data)
        elif subtopic in ("raw", "port_a", "port_b", "run_summary"):
            self.telem("event", {"topic": subtopic, **data})

    async def _action_status(self, st):
        rid = str(st.get("request_id") or "")
        state, action = st.get("state"), st.get("action")
        message = st.get("message", "")
        p = self.pending.get(rid)
        if p is None:
            # unit-side action we didn't send (PLC / local latch): still record
            self.telem("event", {"topic": "action_status", **st})
            return
        if state == "started":
            if not p["acked"]:
                p["acked"] = True
                await self._send({"t": "ACK", "command_id": rid,
                                  "accepted": True})
            self.telem("state", {"to": STATE_FOR_ACTION.get(action, action)},
                       command_id=rid)
        elif state == "progress":
            await self._send({"t": "PROGRESS", "command_id": rid,
                              "pct": int(st.get("progress") or 0),
                              "message": message})
        elif state in ("completed", "failed"):
            if not p["acked"]:
                await self._send({"t": "ACK", "command_id": rid,
                                  "accepted": True})
            data = {"action": action, "message": message}
            if st.get("extra"):
                data["extra"] = st["extra"]
            await self._send({"t": "RESULT", "command_id": rid,
                              "status": ("succeeded" if state == "completed"
                                         else "failed"),
                              "data": data})
            self.telem("state", {"to": "idle"}, command_id=rid)
            self.pending.pop(rid, None)
        elif state == "rejected":
            if p["acked"]:
                await self._send({"t": "RESULT", "command_id": rid,
                                  "status": "failed",
                                  "data": {"action": action,
                                           "message": message}})
            else:
                await self._send({"t": "ACK", "command_id": rid,
                                  "accepted": False, "reason": message})
            self.pending.pop(rid, None)


def main():
    cfg = load_config()
    hub_host, hub_port = cfg.get("hub", "127.0.0.1:7300").rsplit(":", 1)
    b_host, b_port = cfg.get("broker", "127.0.0.1:1883").rsplit(":", 1)
    offline = float(cfg.get("offline_after_s", 90))
    units: dict[str, ShimUnit] = {}

    def on_message(topic, payload):
        parts = topic.split("/")
        if len(parts) == 3 and parts[0] == "tele" and parts[1] in units:
            units[parts[1]].on_tele(parts[2], payload)

    mqtt = MQTTClient(b_host, int(b_port), client_id="uii-mqtt-shim",
                      on_message=on_message)

    async def _run():
        for pi_id, ucfg in cfg["units"].items():
            units[pi_id] = ShimUnit(pi_id, ucfg, mqtt,
                                    (hub_host, int(hub_port)), offline)
            await mqtt.subscribe(f"tele/{pi_id}/#")
        print(f"[mqtt-shim] {len(units)} unit(s) · broker {b_host}:{b_port} · "
              f"hub {hub_host}:{hub_port} · offline_after {offline:.0f}s")
        await mqtt.run()

    asyncio.run(_run())


if __name__ == "__main__":
    main()
