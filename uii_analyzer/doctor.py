"""doctor — is this box recognising its units? One screen, in words.

    uii doctor [--listen-s 12] [--config PATH] [--json]
    python3 -m uii_analyzer.doctor

Checks, each PASS / WARN / FAIL / SKIP with a sentence a site engineer can
act on:

  config     the shim config loads and validates
  services   uii-hub, uii-mqtt-shim (and mosquitto when a broker is local)
             are active + enabled; nothing in `systemctl --failed`   [Linux]
  network    every broker address is loopback or on a directly connected
             subnet (the eth0/eth1 and 192.168 vs 10.10.11 trap)      [Linux]
  hub        the hub API answers; the southbound port accepts
  broker     each configured broker accepts a TCP connect
  units      heartbeats HEARD on each broker vs the unit keys CONFIGURED —
             the `nh4mod` vs `nh4mod-01` trap, said out loud
  adopted    the hub shows each configured unit OPERATIONAL and fresh
  clocks     hub NTP-synced [Linux]; unit heartbeat ts within 5 min of hub

Exit 0 PASS · 1 WARN · 2 FAIL. Stdlib only; read-only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from .mqtt_min import MQTTClient
from .mqtt_shim import (LEGACY_DEFAULT_PI_ID, config_path, load_config,
                        options, parse_addr, parse_unit_ts, validate_config)

RANK = {"PASS": 0, "SKIP": 0, "WARN": 1, "FAIL": 2}
FRESH_S = 30.0          # a unit heartbeats every 5 s; 30 s is stale
SKEW_WARN_S = 300.0


def _check(cid: str, status: str, detail: str) -> dict:
    return {"id": cid, "status": status, "detail": detail}


def _linux() -> bool:
    return platform.system() == "Linux" and shutil.which("systemctl") is not None


def _run(cmd: list[str], timeout=10) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def _tcp_open(host: str, port: int, timeout=3.0) -> Optional[str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except OSError as e:
        return str(e)


def _api(base: str, path: str, token: Optional[str]) -> tuple[int, dict]:
    req = urllib.request.Request(base + path)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return 0, {"error": str(e)}


def _local_subnets() -> list[ipaddress.IPv4Network]:
    rc, out = _run(["ip", "-o", "-4", "addr", "show"])
    nets = []
    if rc != 0:
        return nets
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            cidr = parts[parts.index("inet") + 1]
            try:
                nets.append(ipaddress.ip_interface(cidr).network)
            except ValueError:
                pass
    return nets


async def _listen(broker: str, seconds: float) -> dict[str, dict]:
    """Heartbeats heard on one broker: pi_id -> {n, mode, ts, last}."""
    host, port = parse_addr(broker)
    heard: dict[str, dict] = {}

    def on_message(topic, payload):
        parts = topic.split("/")
        if len(parts) != 3 or parts[0] != "tele" or parts[2] != "heartbeat":
            return
        try:
            data = json.loads(payload.decode())
        except (ValueError, UnicodeDecodeError):
            data = {}
        h = heard.setdefault(parts[1], {"n": 0})
        h["n"] += 1
        h["mode"] = data.get("mode")
        h["ts"] = data.get("ts")
        h["last"] = time.time()

    client = MQTTClient(host, port, client_id="uii-doctor",
                        on_message=on_message, log=lambda _m: None)
    await client.subscribe("tele/+/heartbeat")
    task = asyncio.ensure_future(client.run())
    try:
        await asyncio.sleep(seconds)
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    return heard


def collect(hub_url: Optional[str] = None, token: Optional[str] = None,
            config_path_: Optional[str] = None, listen_s: float = 12.0
            ) -> list[dict]:
    checks: list[dict] = []
    base = (hub_url or os.environ.get("UII_HUB_URL", "http://127.0.0.1:8400")
            ).rstrip("/")
    token = token or os.environ.get("UII_TOKEN")
    path = config_path_ or config_path()

    # -- config -------------------------------------------------------------
    cfg = None
    try:
        cfg = load_config(path)
        errors = validate_config(cfg)
    except (OSError, ValueError) as e:
        errors = [f"cannot read {path}: {e}"]
    if errors:
        checks.append(_check("config", "FAIL", "; ".join(errors)))
        units: dict[str, dict] = {}
        opts = options(cfg if isinstance(cfg, dict) else {})
    else:
        opts = options(cfg)
        units = cfg["units"]
        desc = ", ".join(
            f"{k} -> {u.get('slot')} ({u.get('analyte', 'NH4')}) via "
            f"{u.get('broker') or opts['broker']}" for k, u in units.items())
        checks.append(_check("config", "PASS",
                             f"{path}: {len(units)} unit(s): {desc}"))
    brokers = sorted({str(u.get("broker") or opts["broker"])
                      for u in units.values()} or {opts["broker"]})
    hub_host, hub_port = parse_addr(opts["hub"])

    # -- services (Linux) -----------------------------------------------------
    if _linux():
        names = ["uii-hub", "uii-mqtt-shim"]
        if any(parse_addr(b)[0] in ("127.0.0.1", "localhost") for b in brokers):
            names.append("mosquitto")
        bad, good = [], []
        for n in names:
            _rc, active = _run(["systemctl", "is-active", n])
            _rc, enabled = _run(["systemctl", "is-enabled", n])
            (good if active == "active" else bad).append(
                f"{n} {active}/{enabled}")
        _rc, failed = _run(["systemctl", "--failed", "--no-legend", "--plain"])
        failed_units = [ln.split()[0] for ln in failed.splitlines() if ln.strip()]
        if bad:
            checks.append(_check("services", "FAIL", "; ".join(bad + good)))
        elif failed_units:
            checks.append(_check("services", "WARN",
                                 f"{', '.join(good)}; failed units: "
                                 f"{', '.join(failed_units)}"))
        else:
            checks.append(_check("services", "PASS",
                                 f"{', '.join(good)}; nothing failed"))
    else:
        checks.append(_check("services", "SKIP", "not Linux/systemd"))

    # -- network (Linux) ------------------------------------------------------
    if _linux():
        nets = _local_subnets()
        off = []
        for b in brokers:
            host = parse_addr(b)[0]
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                continue                      # a hostname; DNS decides
            if ip.is_loopback or any(ip in n for n in nets):
                continue
            off.append(f"broker {host} is not on any directly connected subnet "
                       f"(local: {', '.join(str(n) for n in nets) or 'none'})")
        checks.append(_check("network", "FAIL" if off else "PASS",
                             "; ".join(off) if off else
                             f"broker(s) reachable at layer 2: {', '.join(brokers)}"))
    else:
        checks.append(_check("network", "SKIP", "not Linux"))

    # -- hub ------------------------------------------------------------------
    code, sysinfo = _api(base, "/v1/system", token)
    sb_err = _tcp_open(hub_host, hub_port)
    if code == 200:
        detail = (f"{sysinfo.get('hub')} at {base}, auth {sysinfo.get('auth')}, "
                  f"uptime {sysinfo.get('uptime_s', '?')}s")
        if sb_err:
            checks.append(_check("hub", "FAIL",
                                 f"{detail}; southbound {opts['hub']} refused: {sb_err}"))
        else:
            checks.append(_check("hub", "PASS", f"{detail}; southbound {opts['hub']} open"))
    elif code == 401:
        checks.append(_check("hub", "WARN",
                             f"{base} answers but needs a token (set UII_TOKEN); "
                             f"southbound {opts['hub']} "
                             f"{'refused: ' + sb_err if sb_err else 'open'}"))
    else:
        checks.append(_check("hub", "FAIL",
                             f"{base} unreachable: {sysinfo.get('error', code)}"))
    hub_ok = code == 200

    # -- brokers + heard --------------------------------------------------------
    heard_by_broker: dict[str, dict[str, dict]] = {}
    for b in brokers:
        host, port = parse_addr(b)
        err = _tcp_open(host, port)
        if err:
            checks.append(_check("broker", "FAIL", f"{b} refused: {err}"))
            continue
        try:
            heard = asyncio.run(_listen(b, listen_s))
        except Exception as e:      # never let the listener kill the report
            heard = {}
            checks.append(_check("broker", "WARN", f"{b} listen failed: {e}"))
            continue
        heard_by_broker[b] = heard
        checks.append(_check("broker", "PASS",
                             f"{b} open; heard {len(heard)} unit(s) in {listen_s:.0f}s"))

    # -- units: heard vs configured ----------------------------------------------
    if units:
        problems, fine = [], []
        for pi_id, u in units.items():
            b = str(u.get("broker") or opts["broker"])
            heard = heard_by_broker.get(b, {})
            if pi_id in heard:
                h = heard[pi_id]
                fine.append(f"{pi_id} heard on {b} ({h['n']} heartbeats, "
                            f"mode {h.get('mode')})")
            else:
                others = sorted(k for k in heard if k not in units)
                hint = (f"; heard instead: {', '.join(others)} — the unit key must "
                        f"equal the unit's PI_ID (legacy default "
                        f"'{LEGACY_DEFAULT_PI_ID}')" if others else
                        "; nothing heartbeating there — unit off, wrong broker, "
                        "or gateway not running")
                problems.append(f"configured '{pi_id}' never heard on {b}{hint}")
        extra = sorted(k for b, h in heard_by_broker.items() for k in h
                       if k not in units)
        if problems:
            checks.append(_check("units", "FAIL", "; ".join(problems + fine)))
        elif extra:
            checks.append(_check("units", "WARN",
                                 f"{'; '.join(fine)}; also heard but not configured: "
                                 f"{', '.join(extra)}"))
        else:
            checks.append(_check("units", "PASS", "; ".join(fine)))

    # -- adopted (hub view) --------------------------------------------------------
    if units and hub_ok:
        code, mods = _api(base, "/v1/modules", token)
        items = {m.get("id"): m for m in mods.get("items", [])} if code == 200 else {}
        bad, good = [], []
        for pi_id in units:
            m = items.get(pi_id)
            if m is None:
                heard_here = any(pi_id in h for h in heard_by_broker.values())
                bad.append(f"{pi_id} unknown to the hub"
                           + (" although heard on the broker — check "
                              "`journalctl -u uii-mqtt-shim`" if heard_here else
                              " (never heard either)"))
                continue
            age = m.get("last_seen_s_ago")
            fresh = isinstance(age, (int, float)) and age < FRESH_S
            if m.get("state") == "OPERATIONAL" and fresh:
                good.append(f"{pi_id} OPERATIONAL in {m.get('slot')} as "
                            f"{m.get('role')}, mode {m.get('mode')}, seen {age:.0f}s ago")
            else:
                bad.append(f"{pi_id} {m.get('state')} (seen {age}s ago)")
        checks.append(_check("adopted", "FAIL" if bad else "PASS",
                             "; ".join(bad + good)))

    # -- clocks -----------------------------------------------------------------
    notes, worst = [], "PASS"
    if _linux():
        _rc, ntp = _run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        if ntp.strip() == "yes":
            notes.append("hub NTP synced")
        else:
            notes.append(f"hub NTP not synced ({ntp or 'unknown'})")
            worst = "WARN"
    for b, heard in heard_by_broker.items():
        for pi_id, h in heard.items():
            epoch = parse_unit_ts(h.get("ts"))
            if epoch is None:
                continue
            skew = h.get("last", time.time()) - epoch
            if abs(skew) > SKEW_WARN_S:
                notes.append(f"{pi_id} clock {skew:+.0f}s vs hub (says {h.get('ts')})")
                worst = "WARN"
            else:
                notes.append(f"{pi_id} clock within {abs(skew):.0f}s")
    checks.append(_check("clocks", worst if notes else "SKIP",
                         "; ".join(notes) or "no heartbeat timestamps seen"))
    return checks


def summarize(checks: list[dict]) -> tuple[str, int]:
    worst = max((RANK[c["status"]] for c in checks), default=0)
    fails = sum(c["status"] == "FAIL" for c in checks)
    warns = sum(c["status"] == "WARN" for c in checks)
    verdict = ("FAIL" if worst == 2 else "WARN" if worst == 1 else "PASS")
    return f"{verdict} ({fails} fail, {warns} warn)", worst


def run(hub_url: Optional[str] = None, token: Optional[str] = None,
        config_path: Optional[str] = None, listen_s: float = 12.0,
        as_json: bool = False) -> int:
    checks = collect(hub_url, token, config_path, listen_s)
    verdict, code = summarize(checks)
    if as_json:
        print(json.dumps({"verdict": verdict.split()[0], "checks": checks},
                         indent=2))
        return code
    base = (hub_url or os.environ.get("UII_HUB_URL", "http://127.0.0.1:8400"))
    print(f"uii doctor · hub {base} · config {config_path or config_path_()}")
    for c in checks:
        print(f"{c['status']:<5} {c['id']:<9} {c['detail']}")
    print(f"RESULT: {verdict}")
    return code


def config_path_() -> str:
    return config_path()


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="uii-doctor", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hub", default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--listen-s", type=float, default=12.0)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    return run(a.hub, a.token, a.config, a.listen_s, a.json)


if __name__ == "__main__":
    sys.exit(main())
