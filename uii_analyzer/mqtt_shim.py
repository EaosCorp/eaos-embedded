"""mqtt_shim — presents legacy MQTT NH4MOD units as UII modules.

The Stage-1 shim from tools/legacy-shim.md, revived 2026-08-25 for the
POC era: a unit still running the legacy serial_mqtt_gateway (northbound
MQTT, A. Williamson 2026-03) keeps running unmodified, and this service
gives it a UII face — it dials the hub as the module, announces a manifest,
and translates both directions:

  hub CMD {type, params}       -> cmd/{pi}/action {"action", "request_id", **params}
                                  (legacy takes std_conc at the TOP level)
  tele/{pi}/action_status      -> ACK / PROGRESS / RESULT  (request_id == command_id)
  tele/{pi}/data (adc_capture) -> TELEM observation, channel detector_raw
  tele/{pi}/results            -> TELEM observation, channel <analyte>
                                  (mg/L from the unit's ONBOARD cal math —
                                  declared derived_by_hub: false)
  tele/{pi}/run_summary · raw · port_a/b -> TELEM event
  tele/{pi}/heartbeat          -> TELEM health (+ clock_skew_s)

Topology (decided 2026-09-04, HRSD Nansemond): a legacy unit runs its OWN
mosquitto — the gateway's default mqtt_host is 127.0.0.1 — so the shim
subscribes to the UNIT's broker; nothing on the unit changes at adoption.
Each unit may name its own `broker`; the top-level `broker` is the default
(also the right value when a unit has been repointed at the hub box).

A unit appears on the hub when its first MQTT message is seen and says BYE
after offline_after_s of silence, so `uii modules` reflects what is
actually alive. The unit key in the config MUST equal the unit's PI_ID
(legacy default "nh4mod") — that is the topic prefix the shim listens on.

Robustness rules:
  - PROGRESS is forwarded only when (pct, message) changes; the legacy
    gateway re-publishes the same step about once a second.
  - a command the unit never answers is NACKed after ack_timeout_s, so the
    hub gets its one terminal result instead of waiting for expiry.
  - the unit's heartbeat `ts` is compared with hub time; the skew rides
    along in the health record and is logged when it crosses
    clock_skew_warn_s (the field unit's clock was found frozen at July).
  - one journal line per command: type, id, outcome, duration.

Config: /etc/uii/mqtt-shim.json (or $UII_SHIM_CONFIG)
Run:    python3 -m uii_analyzer.mqtt_shim          (--check validates and exits)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from uii.protocol import PROTO, encode, now_iso

from .mqtt_min import MQTTClient

STATE_FOR_ACTION = {"prime": "priming", "calibrate": "calibrating",
                    "sample": "sampling"}
LEGACY_DEFAULT_PI_ID = "nh4mod"
DEFAULT_CONFIG_PATH = "/etc/uii/mqtt-shim.json"
DEFAULTS = {
    "hub": "127.0.0.1:7300",
    "broker": "127.0.0.1:1883",
    "offline_after_s": 90.0,
    "ack_timeout_s": 15.0,
    "keepalive_s": 30.0,
    "clock_skew_warn_s": 300.0,
}
HUB_UNREACHABLE_LOG_EVERY_S = 60.0


def log(msg: str) -> None:
    print(msg, flush=True)


# -- config -----------------------------------------------------------------

def config_path() -> str:
    return os.environ.get("UII_SHIM_CONFIG", DEFAULT_CONFIG_PATH)


def load_config(path: Optional[str] = None) -> dict:
    with open(path or config_path()) as f:
        return json.load(f)


def parse_addr(text: str) -> tuple[str, int]:
    host, port = str(text).rsplit(":", 1)
    return host, int(port)


def validate_config(cfg: dict) -> list[str]:
    """Every problem in one pass, in words a site engineer can act on."""
    errors: list[str] = []
    if not isinstance(cfg, dict):
        return ["config must be a JSON object"]
    for key in ("hub", "broker"):
        if key in cfg:
            try:
                parse_addr(cfg[key])
            except (ValueError, TypeError):
                errors.append(f"'{key}' must be host:port, got {cfg[key]!r}")
    for key in ("offline_after_s", "ack_timeout_s", "keepalive_s",
                "clock_skew_warn_s"):
        if key in cfg:
            try:
                if float(cfg[key]) <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(f"'{key}' must be a positive number, "
                              f"got {cfg[key]!r}")
    units = cfg.get("units")
    if not isinstance(units, dict) or not units:
        errors.append("'units' must be a non-empty object keyed by each "
                      f"unit's PI_ID (legacy default '{LEGACY_DEFAULT_PI_ID}')")
        return errors
    for pi_id, u in units.items():
        if not isinstance(u, dict):
            errors.append(f"unit '{pi_id}': must be an object")
            continue
        if not pi_id or any(c in pi_id for c in "/+#"):
            errors.append(f"unit '{pi_id}': key must be a bare pi_id "
                          f"(it becomes the topic prefix tele/{pi_id}/#)")
        for field in ("slot", "serial"):
            if not u.get(field):
                errors.append(f"unit '{pi_id}': missing '{field}'")
        if "broker" in u:
            try:
                parse_addr(u["broker"])
            except (ValueError, TypeError):
                errors.append(f"unit '{pi_id}': 'broker' must be host:port, "
                              f"got {u['broker']!r}")
    return errors


def options(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if key in cfg:
            out[key] = type(DEFAULTS[key])(cfg[key])
    return out


# -- helpers ----------------------------------------------------------------

_BASIC_OFFSET = re.compile(r"([+-])(\d{2})(\d{2})$")


def parse_unit_ts(ts) -> Optional[float]:
    """Legacy '2026-07-08T18:25:34+0000' or bench '...Z' -> epoch seconds."""
    if not isinstance(ts, str) or not ts:
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    s = _BASIC_OFFSET.sub(r"\1\2:\3", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def short(command_id: str) -> str:
    return str(command_id)[:8]


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


# -- one unit ---------------------------------------------------------------

class ShimUnit:
    """One legacy unit's UII face: a hub connection plus the translation."""

    def __init__(self, pi_id: str, cfg: dict, mqtt: MQTTClient,
                 hub_addr: tuple[str, int], opts: dict):
        self.pi_id = pi_id
        self.slot = cfg["slot"]
        self.analyte = cfg.get("analyte", "NH4")
        self.serial = cfg["serial"]
        self.module_type = cfg.get("type", f"nh4mod-{self.analyte.lower()}")
        self.mqtt = mqtt
        self.hub_addr = hub_addr
        self.offline_after_s = float(opts["offline_after_s"])
        self.ack_timeout_s = float(opts["ack_timeout_s"])
        self.skew_warn_s = float(opts["clock_skew_warn_s"])
        self.last_seen = 0.0
        self.writer = None
        self.local_seq = 0
        # command_id -> {type, started_at, acked, last_progress, timer}
        self.pending: dict[str, dict] = {}
        self._task: Optional[asyncio.Future] = None
        self.hub_up = False
        self._hub_fail_logged = 0.0
        self.clock_skew_s: Optional[float] = None
        self._skew_flagged = False

    # -- northbound (to the hub) --------------------------------------------

    def seen(self):
        self.last_seen = time.time()
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    async def _send(self, msg: dict):
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

    def _hub_down(self, reason: str):
        if self.hub_up:
            log(f"[{self.pi_id}] hub connection lost: {reason}; reconnecting")
            self.hub_up = False
            self._hub_fail_logged = time.time()
        elif time.time() - self._hub_fail_logged >= HUB_UNREACHABLE_LOG_EVERY_S:
            log(f"[{self.pi_id}] hub {self.hub_addr[0]}:{self.hub_addr[1]} "
                f"unreachable: {reason}; retrying")
            self._hub_fail_logged = time.time()

    async def _run(self):
        backoff = 1.0
        while time.time() - self.last_seen < self.offline_after_s:
            reason = "closed by hub"
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
                    log(f"[{self.pi_id}] quarantined: {resp.get('reason')}")
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
                log(f"[{self.pi_id}] adopted as {resp.get('role')} (via shim)")
                self.hub_up = True
                backoff = 1.0
                while True:
                    # silence check every pass — hub PINGs keep lines flowing,
                    # so the read timeout alone can never prove the UNIT is gone
                    if time.time() - self.last_seen >= self.offline_after_s:
                        log(f"[{self.pi_id}] silent on MQTT "
                            f"{self.offline_after_s:.0f}s — BYE")
                        await self._send({"t": "BYE",
                                          "reason": "unit silent on MQTT"})
                        writer.close()
                        self.writer = None
                        self.hub_up = False
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
                    json.JSONDecodeError) as e:
                reason = f"{type(e).__name__}: {e}".rstrip(": ")
            self.writer = None
            self._hub_down(reason)
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 1.7)

    # -- hub CMD -> legacy MQTT ---------------------------------------------

    async def deliver(self, cmd: dict):
        cmd_id, cmd_type = cmd["command_id"], cmd["type"]
        params = cmd.get("params") or {}
        entry = {"type": cmd_type, "started_at": time.time(), "acked": False,
                 "last_progress": None, "timer": None}
        self.pending[cmd_id] = entry
        payload = {"action": cmd_type, "request_id": cmd_id, **params}
        if not await self.mqtt.publish(f"cmd/{self.pi_id}/action", payload):
            self.pending.pop(cmd_id, None)
            reason = f"unit's MQTT broker {self.mqtt.addr} unreachable"
            await self._send({"t": "ACK", "command_id": cmd_id,
                              "accepted": False, "reason": reason})
            log(f"[{self.pi_id}] {cmd_type} {short(cmd_id)} not sent: {reason}")
            return
        entry["timer"] = asyncio.ensure_future(self._ack_timeout(cmd_id))

    async def _ack_timeout(self, cmd_id: str):
        await asyncio.sleep(self.ack_timeout_s)
        p = self.pending.get(cmd_id)
        if p is None or p["acked"]:
            return
        self.pending.pop(cmd_id, None)
        reason = f"unit did not respond within {self.ack_timeout_s:.0f}s"
        await self._send({"t": "ACK", "command_id": cmd_id,
                          "accepted": False, "reason": reason})
        log(f"[{self.pi_id}] {p['type']} {short(cmd_id)} rejected: {reason}")

    def _settle(self, cmd_id: str) -> Optional[dict]:
        p = self.pending.pop(cmd_id, None)
        if p and p["timer"]:
            p["timer"].cancel()
        return p

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
            self._note_clock(data.get("ts"))
            if self.clock_skew_s is not None:
                data = {**data, "clock_skew_s": round(self.clock_skew_s, 1)}
            self.telem("health", data)
        elif subtopic in ("raw", "port_a", "port_b", "run_summary"):
            self.telem("event", {"topic": subtopic, **data})

    def _note_clock(self, ts):
        unit_epoch = parse_unit_ts(ts)
        if unit_epoch is None:
            return
        self.clock_skew_s = time.time() - unit_epoch
        over = abs(self.clock_skew_s) > self.skew_warn_s
        if over and not self._skew_flagged:
            log(f"[{self.pi_id}] clock skew {self.clock_skew_s:+.0f}s vs hub "
                f"(unit says {ts}); the unit needs a time source")
        elif not over and self._skew_flagged:
            log(f"[{self.pi_id}] clock skew back within "
                f"{self.skew_warn_s:.0f}s ({self.clock_skew_s:+.0f}s)")
        self._skew_flagged = over

    async def _ack(self, p: dict, rid: str):
        if not p["acked"]:
            p["acked"] = True
            await self._send({"t": "ACK", "command_id": rid, "accepted": True})

    async def _action_status(self, st: dict):
        rid = str(st.get("request_id") or "")
        state, action = st.get("state"), st.get("action")
        message = st.get("message", "")
        p = self.pending.get(rid)
        if p is None:
            # unit-side action we didn't send (PLC / local latch): still record
            self.telem("event", {"topic": "action_status", **st})
            return
        if state == "started":
            await self._ack(p, rid)
            self.telem("state", {"to": STATE_FOR_ACTION.get(action, action)},
                       command_id=rid)
        elif state == "progress":
            await self._ack(p, rid)
            key = (int(st.get("progress") or 0), message)
            if key == p["last_progress"]:
                return
            p["last_progress"] = key
            await self._send({"t": "PROGRESS", "command_id": rid,
                              "pct": key[0], "message": message})
        elif state in ("completed", "failed"):
            await self._ack(p, rid)
            self._settle(rid)
            data = {"action": action, "message": message}
            if st.get("extra"):
                data["extra"] = st["extra"]
            status = "succeeded" if state == "completed" else "failed"
            await self._send({"t": "RESULT", "command_id": rid,
                              "status": status, "data": data})
            self.telem("state", {"to": "idle"}, command_id=rid)
            log(f"[{self.pi_id}] {p['type']} {short(rid)} {status} in "
                f"{time.time() - p['started_at']:.0f}s"
                f"{': ' + message if status == 'failed' and message else ''}")
        elif state == "rejected":
            self._settle(rid)
            if p["acked"]:
                await self._send({"t": "RESULT", "command_id": rid,
                                  "status": "failed",
                                  "data": {"action": action,
                                           "message": message}})
            else:
                await self._send({"t": "ACK", "command_id": rid,
                                  "accepted": False, "reason": message})
            log(f"[{self.pi_id}] {p['type']} {short(rid)} rejected: {message}")


# -- service ----------------------------------------------------------------

def build(cfg: dict) -> tuple[dict, dict[str, MQTTClient], dict[str, ShimUnit]]:
    """Wire units to brokers. Returns (opts, clients by broker, units by pi_id)."""
    opts = options(cfg)
    hub_addr = parse_addr(opts["hub"])
    clients: dict[str, MQTTClient] = {}
    units: dict[str, ShimUnit] = {}
    by_broker: dict[str, dict[str, ShimUnit]] = {}

    def client_for(broker: str) -> MQTTClient:
        if broker not in clients:
            host, port = parse_addr(broker)
            members = by_broker.setdefault(broker, {})

            def on_message(topic, payload, _members=members):
                parts = topic.split("/")
                if len(parts) == 3 and parts[0] == "tele" and parts[1] in _members:
                    _members[parts[1]].on_tele(parts[2], payload)

            clients[broker] = MQTTClient(host, port, client_id="uii-mqtt-shim",
                                         on_message=on_message,
                                         keepalive=opts["keepalive_s"], log=log)
        return clients[broker]

    for pi_id, ucfg in cfg["units"].items():
        broker = str(ucfg.get("broker") or opts["broker"])
        client = client_for(broker)
        unit = ShimUnit(pi_id, ucfg, client, hub_addr, opts)
        units[pi_id] = unit
        by_broker[broker][pi_id] = unit
    return opts, clients, units


async def serve(cfg: dict):
    opts, clients, units = build(cfg)
    for pi_id, unit in units.items():
        await unit.mqtt.subscribe(f"tele/{pi_id}/#")
    log(f"[mqtt-shim] hub {opts['hub']} · offline_after {opts['offline_after_s']:.0f}s"
        f" · ack_timeout {opts['ack_timeout_s']:.0f}s"
        f" · keepalive {opts['keepalive_s']:.0f}s")
    for pi_id, unit in units.items():
        log(f"[mqtt-shim] unit {pi_id} -> {unit.slot} ({unit.analyte}) "
            f"via broker {unit.mqtt.addr}")
    log(f"[mqtt-shim] unit keys must equal each unit's PI_ID (legacy default "
        f"'{LEGACY_DEFAULT_PI_ID}'); a unit appears on its first heartbeat")
    await asyncio.gather(*(c.run() for c in clients.values()))


def main(argv: Optional[list[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    check_only = "--check" in argv
    path = config_path()
    try:
        cfg = load_config(path)
    except (OSError, ValueError) as e:
        log(f"[mqtt-shim] cannot read config {path}: {e}")
        return 2
    errors = validate_config(cfg)
    if errors:
        for e in errors:
            log(f"[mqtt-shim] config error ({path}): {e}")
        log("[mqtt-shim] see deploy/mqtt-shim.json.example; not starting")
        return 2
    if check_only:
        opts, _clients, units = build(cfg)
        for pi_id, unit in units.items():
            log(f"[mqtt-shim] ok: unit {pi_id} -> {unit.slot} ({unit.analyte}) "
                f"via broker {unit.mqtt.addr}; hub {opts['hub']}")
        return 0
    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
