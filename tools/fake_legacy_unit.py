"""A stand-in NH4MOD unit — the legacy MQTT gateway at the wire, simulated
chemistry underneath.

Same topics, action names, payload shapes, and mode semantics as Arba's
serial_mqtt_gateway.py (2026-09 NH4 build; ancestry in the 2026-03
nh4_nox_po4 build): BRIDGE on boot, take_control latches ENDPOINT,
prime/prime2/calibrate/sample rejected outside ENDPOINT, "Action queue
full" while one runs, std_conc at the top level of the command payload,
started/progress/completed/failed/rejected action_status states, results
as {type, vin, absorbance, fit, nh4_mgL, error}. Serial ports and the
ADS1115 are replaced by refmod's hidden-slope physics, so a calibrate
genuinely has to recover slope 25.

For bench-testing the mqtt-shim path with no unit on the LAN:

  FAKE_PI_ID=nh4mod FAKE_ANALYTE=NH4 FAKE_BROKER=127.0.0.1:1883 \
  FAKE_SPEED=30 python3 tools/fake_legacy_unit.py

Field-faithful knobs (all optional):
  FAKE_PROGRESS_REPEAT=N   re-publish each progress step N times, like the
                           real gateway does about once a second (shim must
                           forward it once)
  FAKE_DEAF=1              heartbeats only; never answers a command (the
                           shim must NACK after its ack timeout)
  FAKE_CLOCK_OFFSET_S=-3600  shift every `ts` (the field unit's clock was
                           found months behind)
  FAKE_HEARTBEAT_S=1.0     heartbeat period (default 10/speed, min 0.5)
  FAKE_KEEPALIVE_S=30      MQTT keepalive (the unit drops a silent broker
                           after 1.5x this)
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uii_analyzer.mqtt_min import MQTTClient  # noqa: E402

TRUE_SLOPE = 25.0
I0_VOLTS = 2.400


CLOCK_OFFSET_S = float(os.environ.get("FAKE_CLOCK_OFFSET_S", 0) or 0)


def now_iso() -> str:
    t = time.time() + CLOCK_OFFSET_S
    return (time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t))
            + f".{int(t*1000)%1000:03d}Z")


class FakeUnit:
    def __init__(self):
        e = os.environ
        self.pi_id = e.get("FAKE_PI_ID", "nh4mod")
        self.analyte = e.get("FAKE_ANALYTE", "NH4")
        host, port = e.get("FAKE_BROKER", "127.0.0.1:1883").rsplit(":", 1)
        self.speed = max(float(e.get("FAKE_SPEED", 30)), 1e-6)
        self.progress_repeat = max(int(e.get("FAKE_PROGRESS_REPEAT", 1)), 1)
        self.deaf = e.get("FAKE_DEAF", "") not in ("", "0")
        self.heartbeat_s = float(e.get("FAKE_HEARTBEAT_S", 0) or
                                 max(0.5, 10 / self.speed))
        self.mode = "BRIDGE"
        self.latched = False
        self.running = False
        self.slope = None            # recovered by calibrate, like the field
        self.intercept = 0.0
        self.cal_ts = None
        self.t0 = time.time()
        self._pending_i0: dict[str, float] = {}
        self.mqtt = MQTTClient(host, int(port),
                               client_id=f"{self.pi_id}-gateway",
                               on_message=self.on_message,
                               keepalive=float(e.get("FAKE_KEEPALIVE_S", 30)))

    # -- physics (refmod's) --------------------------------------------------

    def true_conc(self) -> float:
        t = time.time() - self.t0
        return max(0.05, 4.5 + 1.1 * math.sin(t / 90.0) + random.gauss(0, 0.05))

    def volts(self, name: str, std_conc=None) -> float:
        conc = (0.0 if "_DIW_" in name
                else float(std_conc or 5.0) if "_STD_" in name
                else self.true_conc())
        pair = name.replace("_I0", "").replace("_I1", "")
        if "_I0" in name:
            i0 = I0_VOLTS * (1 + random.gauss(0, 0.002))
            self._pending_i0[pair] = i0
            return round(i0, 5)
        i0 = self._pending_i0.pop(pair, I0_VOLTS)
        return round(i0 * (10 ** -(conc / TRUE_SLOPE))
                     * (1 + random.gauss(0, 0.002)), 5)

    # -- publishers (legacy payload shapes) ----------------------------------

    def pub(self, sub: str, payload: dict):
        asyncio.ensure_future(
            self.mqtt.publish(f"tele/{self.pi_id}/{sub}", payload))

    def status(self, rid, action, state, message, progress, extra=None):
        p = {"ts": now_iso(), "request_id": rid, "action": action,
             "state": state, "message": message, "progress": int(progress)}
        if extra:
            p["extra"] = extra
        repeats = self.progress_repeat if state == "progress" else 1
        for _ in range(repeats):
            self.pub("action_status", p)

    def adc_capture(self, rid, action, name, vin):
        self.pub("data", {"ts": now_iso(), "type": "adc_capture",
                          "pi_id": self.pi_id, "request_id": rid,
                          "action": action, "name": name,
                          "vadc": vin, "vin": vin, "ma": None,
                          "signal": {"vadc": vin, "vin": vin, "ma": None},
                          "note": ""})

    # -- command handling (mirrors _on_message + the worker) -----------------

    def on_message(self, topic, payload):
        if topic != f"cmd/{self.pi_id}/action" or self.deaf:
            return
        try:
            obj = json.loads(payload.decode())
        except ValueError:
            obj = {"action": payload.decode(errors="replace")}
        action = str(obj.get("action", "")).lower()
        rid = str(obj.get("request_id", "") or f"{int(time.time())}")

        if action in ("take_control", "endpoint"):
            self.mode, self.latched = "ENDPOINT", True
            self.status(rid, "take_control", "completed", "ENDPOINT latched", 100)
            return
        if action == "bridge":
            self.mode = "BRIDGE"
            self.status(rid, "bridge", "completed", "BRIDGE requested", 100)
            return
        if self.mode != "ENDPOINT":
            self.status(rid, action, "rejected", "Not in ENDPOINT mode", 0)
            return
        if action in ("prime", "prime2", "calibrate", "sample"):
            if self.running:
                self.status(rid, action, "rejected", "Action queue full", 0)
                return
            asyncio.ensure_future(self.run_action(action, rid, obj))
            return
        self.status(rid, action, "rejected", "Unknown action", 0)

    async def run_action(self, action, rid, obj):
        self.running = True
        try:
            if action == "prime":
                await self.run_prime(rid)
            elif action == "prime2":
                await self.run_prime2(rid)
            elif action == "calibrate":
                std_conc = float(obj.get("std_conc", 0.0))
                if std_conc <= 0:
                    self.status(rid, "calibrate", "failed",
                                "std_conc must be > 0", 0)
                else:
                    await self.run_calibrate(rid, std_conc)
            elif action == "sample":
                await self.run_sample(rid)
        finally:
            self.running = False

    async def run_prime(self, rid):
        # the real timeline: ST9 35 @6s, 40 @14s, 41 @197s, 300 s total
        self.status(rid, "prime", "started", "prime started", 0)
        for i, st9 in enumerate((35, 40, 41), 1):
            await asyncio.sleep(8 / self.speed)
            self.status(rid, "prime", "progress",
                        f"Sent ST9:{st9} (OK)", int(i * 30))
        self.status(rid, "prime", "completed", "Prime complete", 100)

    async def run_prime2(self, rid):
        # the 2026-09 short prime: ST9 35 @1s, 88 @5s, 15 s total
        self.status(rid, "prime2", "started", "prime2 started", 0)
        for i, st9 in enumerate((35, 88), 1):
            await asyncio.sleep(4 / self.speed)
            self.status(rid, "prime2", "progress",
                        f"Sent ST9:{st9} (OK)", int(i * 45))
        self.status(rid, "prime2", "completed", "Prime2 complete", 100)

    async def run_calibrate(self, rid, std_conc):
        a = self.analyte
        echo = {"std_conc": std_conc}
        self.status(rid, "calibrate", "started", "calibrate started", 0, echo)
        caps = {}
        names = (f"{a}_CAL_DIW_I0", f"{a}_CAL_DIW_I1",
                 f"{a}_CAL_STD_I0", f"{a}_CAL_STD_I1")
        for i, name in enumerate(names, 1):
            await asyncio.sleep(10 / self.speed)
            v = self.volts(name, std_conc)
            caps[name] = v
            self.adc_capture(rid, "calibrate", name, v)
            self.status(rid, "calibrate", "progress",
                        f"Captured {name}", int(i * 22), echo)
        a_diw = math.log10(caps[names[0]] / caps[names[1]])
        a_std = math.log10(caps[names[2]] / caps[names[3]])
        denom = a_std - a_diw
        if denom <= 0:
            self.status(rid, "calibrate", "failed", "flat calibration", 95, echo)
            return
        self.slope = std_conc / denom
        self.intercept = -self.slope * a_diw
        self.cal_ts = now_iso()
        result = {"ts": self.cal_ts, "type": "calibration",
                  "units": {"concentration": "mg/L"}, "request_id": rid,
                  "std_conc_mgL": std_conc, "vin": caps,
                  "absorbance": {"diw": round(a_diw, 6), "std": round(a_std, 6),
                                 "log_base": 10},
                  "fit": {"slope": round(self.slope, 4),
                          "intercept": round(self.intercept, 6)},
                  "error": ""}
        self.pub("results", result)
        self.pub("run_summary", {"ts": now_iso(), "request_id": rid,
                                 "action": "calibrate", "state": "completed",
                                 "std_conc_mgL": std_conc, "error": ""})
        self.status(rid, "calibrate", "completed", "Calibration complete", 100,
                    echo)

    async def run_sample(self, rid):
        a = self.analyte
        self.status(rid, "sample", "started", "sample started", 0)
        if self.slope is None:
            self.status(rid, "sample", "failed", "calibration incomplete", 0)
            return
        caps = {}
        for i, name in enumerate((f"{a}_SAMP_I0", f"{a}_SAMP_I1"), 1):
            await asyncio.sleep(10 / self.speed)
            v = self.volts(name)
            caps[name] = v
            self.adc_capture(rid, "sample", name, v)
            self.status(rid, "sample", "progress", f"Captured {name}",
                        int(i * 45))
        absorbance = math.log10(caps[f"{a}_SAMP_I0"] / caps[f"{a}_SAMP_I1"])
        value = self.slope * absorbance + self.intercept
        self.pub("results", {"ts": now_iso(), "type": "sample",
                             "units": {"concentration": "mg/L"},
                             "request_id": rid, "cal_ts": self.cal_ts,
                             "vin": caps,
                             "absorbance": {"sample": round(absorbance, 6),
                                            "log_base": 10},
                             "fit": {"slope": round(self.slope, 4),
                                     "intercept": round(self.intercept, 6)},
                             f"{a.lower()}_mgL": round(value, 4), "error": ""})
        self.pub("run_summary", {"ts": now_iso(), "request_id": rid,
                                 "action": "sample", "state": "completed",
                                 "error": ""})
        self.status(rid, "sample", "completed",
                    f"{a} = {value:.3f} mg/L", 100)

    # -- loops ----------------------------------------------------------------

    async def heartbeat_loop(self):
        while True:
            self.pub("heartbeat", {"ts": now_iso(), "pi_id": self.pi_id,
                                   "analyte": self.analyte, "mode": self.mode,
                                   "uptime_s": int(time.time() - self.t0),
                                   "port_a_ok": True, "port_b_ok": True,
                                   "action_running": self.running})
            await asyncio.sleep(self.heartbeat_s)

    async def run(self):
        await self.mqtt.subscribe(f"cmd/{self.pi_id}/action")
        print(f"[fake-unit] {self.pi_id} ({self.analyte}) · broker "
              f"{self.mqtt.host}:{self.mqtt.port} · speed {self.speed}x · "
              f"mode {self.mode}"
              f"{' · deaf' if self.deaf else ''}"
              f"{f' · progress x{self.progress_repeat}' if self.progress_repeat > 1 else ''}"
              f"{f' · clock {CLOCK_OFFSET_S:+.0f}s' if CLOCK_OFFSET_S else ''}",
              flush=True)
        asyncio.ensure_future(self.heartbeat_loop())
        await self.mqtt.run()


if __name__ == "__main__":
    asyncio.run(FakeUnit().run())
