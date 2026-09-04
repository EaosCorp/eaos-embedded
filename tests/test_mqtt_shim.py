"""The legacy-shim path, end to end and under the field's failure modes.

A real mosquitto on an ephemeral port (skipped with a clear message if the
binary is absent), tools/fake_legacy_unit.py as the unit, the shim as a
subprocess reading a temp config, the hub in-process (tests.helpers.Bench).

Field episodes these encode (HRSD Nansemond, 2026-09-04):
  - the unit key must equal the unit's PI_ID (`nh4mod` vs `nh4mod-01`)
  - the gateway re-publishes progress ~1/s; the record wants each step once
  - a unit that lost power leaves a half-open socket; nothing may hang
  - the unit reboots on its own; the hub must re-adopt hands-off
  - the unit's clock was months behind
  - the 2026-09 gateway build adds prime2 (ST9 35 then 88, 15 s) and
    reports results as {vin, absorbance, fit, nh4_mgL, error}
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from tests.helpers import Bench, ROOT, get, post, wait_for

MOSQUITTO = shutil.which("mosquitto")
ROLES = {"slot-1": {"role": "nh4-influent", "analyte": "NH4",
                    "sample_interval_s": 900, "calibrate_std_conc": 5.0,
                    "auto_take_control": False, "auto_calibrate": False}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Broker:
    """mosquitto on 127.0.0.1:<port>, anonymous, QoS-0 traffic only."""

    def __init__(self, tmp: str, port: int | None = None):
        self.port = port or free_port()
        self.conf = os.path.join(tmp, f"mosq-{self.port}.conf")
        with open(self.conf, "w") as f:
            f.write(f"listener {self.port} 127.0.0.1\nallow_anonymous true\n"
                    f"log_type none\n")
        self.proc: subprocess.Popen | None = None

    def start(self):
        self.proc = subprocess.Popen([MOSQUITTO, "-c", self.conf],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        wait_for(lambda: port_open(self.port), timeout=10, what="broker up")
        return self

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=5)
            self.proc = None
        wait_for(lambda: not port_open(self.port), timeout=10, what="broker down")

    def freeze(self):      # SIGSTOP: the TCP socket stays, the peer goes silent
        self.proc.send_signal(signal.SIGSTOP)

    def thaw(self):
        self.proc.send_signal(signal.SIGCONT)


class ShimProc:
    def __init__(self, tmp: str, hub_sb_port: int, broker_port: int,
                 units: dict | None = None, **opts):
        cfg = {"hub": f"127.0.0.1:{hub_sb_port}",
               "broker": f"127.0.0.1:{broker_port}",
               "offline_after_s": 30, "ack_timeout_s": 15,
               "units": units if units is not None else
               {"nh4mod": {"slot": "slot-1", "analyte": "NH4",
                           "serial": "NH4MOD-TEST-0001"}}}
        cfg.update(opts)
        self.config = os.path.join(tmp, f"shim-{free_port()}.json")
        with open(self.config, "w") as f:
            json.dump(cfg, f)
        self.logfile = self.config + ".log"
        self.proc: subprocess.Popen | None = None
        self._out = None

    def start(self):
        env = dict(os.environ, UII_SHIM_CONFIG=self.config, PYTHONPATH=ROOT,
                   PYTHONUNBUFFERED="1")
        self._out = open(self.logfile, "w")
        self.proc = subprocess.Popen([sys.executable, "-m", "uii_analyzer.mqtt_shim"],
                                     cwd=ROOT, env=env,
                                     stdout=self._out, stderr=subprocess.STDOUT)
        return self

    def log(self) -> str:
        try:
            with open(self.logfile) as f:
                return f.read()
        except OSError:
            return ""

    def stop(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait(timeout=5)
            self.proc = None
        if self._out:
            self._out.close()
            self._out = None


def spawn_unit(broker_port: int, pi_id="nh4mod", **extra) -> subprocess.Popen:
    env = dict(os.environ, FAKE_BROKER=f"127.0.0.1:{broker_port}",
               FAKE_PI_ID=pi_id, FAKE_SPEED="50", FAKE_HEARTBEAT_S="0.5",
               PYTHONPATH=ROOT)
    env.update({k: str(v) for k, v in extra.items()})
    return subprocess.Popen([sys.executable, os.path.join(ROOT, "tools",
                                                          "fake_legacy_unit.py")],
                            cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


@unittest.skipIf(MOSQUITTO is None,
                 "mosquitto not installed (brew install mosquitto / apt install mosquitto)")
class ShimPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uii-shim-")
        self.bench = Bench(roles=ROLES, extensions=["analyzer"])
        self.broker = Broker(self.tmp).start()
        self.shim: ShimProc | None = None
        self.unit: subprocess.Popen | None = None

    def tearDown(self):
        for p in (self.unit,):
            if p:
                p.kill()
                p.wait(timeout=3)
        if self.shim:
            self.shim.stop()
        try:
            self.broker.stop()
        except Exception:
            pass
        self.bench.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ---------------------------------------------------------------

    def start_shim(self, **opts) -> ShimProc:
        self.shim = ShimProc(self.tmp, self.bench.hub.sb_port, self.broker.port,
                             **opts).start()
        return self.shim

    def start_unit(self, **extra):
        self.unit = spawn_unit(self.broker.port, **extra)
        return self.unit

    def adopted(self, module="nh4mod", timeout=30):
        wait_for(lambda: self.bench.state_of(module) == "OPERATIONAL",
                 timeout=timeout, what=f"{module} adopted")
        return self.bench.module(module)

    def cmd(self, cmd_type, module="nh4mod", **params) -> str:
        code, resp = post(self.bench.base, "/v1/commands",
                          {"module": module, "type": cmd_type,
                           "params": params, "actor": "user:local"})
        self.assertEqual(code, 202, resp)
        return resp["command_id"]

    def done(self, cid, timeout=30) -> dict:
        wait_for(lambda: get(self.bench.base, f"/v1/commands/{cid}")["state"] == "done",
                 timeout=timeout, what=f"command {cid[:8]} done")
        return get(self.bench.base, f"/v1/commands/{cid}")

    # -- the happy path ----------------------------------------------------------

    def test_adopts_on_first_heartbeat_and_tracks_mode(self):
        self.start_shim()
        self.start_unit()
        m = self.adopted()
        self.assertEqual(m["slot"], "slot-1")
        self.assertEqual(m["role"], "nh4-influent")
        self.assertEqual(m["type"], "nh4mod-nh4")
        wait_for(lambda: self.bench.module("nh4mod").get("mode") == "BRIDGE",
                 what="mode BRIDGE from heartbeat")
        st = self.done(self.cmd("take_control"))
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        wait_for(lambda: self.bench.module("nh4mod").get("mode") == "ENDPOINT",
                 what="mode ENDPOINT after take_control")
        self.assertIn("adopted as nh4-influent", self.shim.log())

    def test_prime_round_trip_forwards_each_progress_step_once(self):
        self.start_shim()
        self.start_unit(FAKE_PROGRESS_REPEAT=5)     # the gateway's ~1/s re-publish
        self.adopted()
        self.done(self.cmd("take_control"))
        cid = self.cmd("prime")
        st = self.done(cid)
        self.assertTrue(st["ack"]["data"]["accepted"])
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        steps = [(p["pct"], p["message"]) for p in st["progress"]]
        self.assertEqual(steps, [(30, "Sent ST9:35 (OK)"), (60, "Sent ST9:40 (OK)"),
                                 (90, "Sent ST9:41 (OK)")],
                         "progress must be forwarded once per distinct step")
        wait_for(lambda: f"prime {cid[:8]} succeeded in" in self.shim.log(),
                 timeout=5, what="one journal line for the command")
        wait_for(lambda: self.bench.module("nh4mod").get("module_state") == "idle",
                 what="module back to idle")

    def test_prime2_is_the_short_prime(self):
        self.start_shim()
        self.start_unit()
        self.adopted()
        declared = {c["type"]: c for c in
                    get(self.bench.base, "/v1/modules/nh4mod/commands")["commands"]}
        self.assertIn("prime2", declared)
        self.assertEqual(declared["prime2"]["preconditions"], ["mode:ENDPOINT"])
        self.done(self.cmd("take_control"))
        cid = self.cmd("prime2")
        st = self.done(cid)
        self.assertEqual(st["result"]["data"]["status"], "succeeded")
        self.assertEqual([(p["pct"], p["message"]) for p in st["progress"]],
                         [(45, "Sent ST9:35 (OK)"), (90, "Sent ST9:88 (OK)")])
        wait_for(lambda: f"prime2 {cid[:8]} succeeded in" in self.shim.log(),
                 timeout=5, what="journal line")

    def test_hub_prerejects_timeline_actions_outside_endpoint(self):
        """Declared precondition: the hub answers 422 from what the heartbeat
        told it, no round-trip to a unit that would only say no."""
        self.start_shim()
        self.start_unit()
        self.adopted()
        wait_for(lambda: self.bench.module("nh4mod").get("mode") == "BRIDGE",
                 what="mode known")
        for cmd_type in ("prime", "prime2", "sample"):
            code, resp = post(self.bench.base, "/v1/commands",
                              {"module": "nh4mod", "type": cmd_type,
                               "params": {}, "actor": "user:local"})
            self.assertEqual(code, 422, resp)
            self.assertIn("requires mode=ENDPOINT", resp.get("detail", ""))
        self.done(self.cmd("take_control"))           # mode commands still pass

    def test_unit_side_rejection_is_one_terminal_result(self):
        """When the unit itself refuses (queue busy), the shim turns the
        rejection into one refused ACK and the hub into one 'rejected' result."""
        self.start_shim()
        self.start_unit(FAKE_SPEED=2)                 # prime takes ~12 s
        self.adopted()
        self.done(self.cmd("take_control"))
        first = self.cmd("prime")
        wait_for(lambda: get(self.bench.base, f"/v1/commands/{first}")["state"]
                 == "running", what="first prime running")
        st = self.done(self.cmd("prime2"), timeout=10)
        self.assertFalse(st["ack"]["data"]["accepted"])
        self.assertEqual(st["result"]["data"]["status"], "rejected")
        self.assertEqual(st["result"]["data"]["reason"], "Action queue full")
        self.assertIn("prime2", self.shim.log())
        self.assertIn("rejected: Action queue full", self.shim.log())

    def test_sample_result_carries_the_units_mgL_as_value(self):
        """2026-09 result shape: nh4_mgL becomes `value`, fit and error ride
        along; a calibration result is recorded with its fit."""
        self.start_shim()
        self.start_unit()
        self.adopted()
        self.done(self.cmd("take_control"))
        self.assertEqual(self.done(self.cmd("calibrate", std_conc=5.0))
                         ["result"]["data"]["status"], "succeeded")
        self.assertEqual(self.done(self.cmd("sample"))["result"]["data"]["status"],
                         "succeeded")

        def unit_results():
            items = get(self.bench.base,
                        "/v1/observations?channel=nh4&module=nh4mod")["items"]
            return [o for o in items if "nh4_mgL" in (o.get("data") or {})] or None
        obs = wait_for(unit_results, timeout=10, what="unit sample result")[-1]
        d = obs["data"]
        self.assertEqual(d["value"], d["nh4_mgL"])
        self.assertEqual(d["unit"], "mg/L")
        self.assertAlmostEqual(d["value"], 4.5, delta=1.5)     # the sim's true conc
        self.assertAlmostEqual(d["fit"]["slope"], 25.0, delta=1.0)
        self.assertEqual(obs["quality"]["status"], "good")

    def test_config_validation_refuses_to_start_with_a_reason(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as f:
            json.dump({"hub": "nope", "offline_after_s": 0,
                       "units": {"tele/x": {"analyte": "NH4"}}}, f)
        r = subprocess.run([sys.executable, "-m", "uii_analyzer.mqtt_shim", "--check"],
                           cwd=ROOT, env=dict(os.environ, UII_SHIM_CONFIG=bad,
                                              PYTHONPATH=ROOT),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 2)
        for needle in ("'hub' must be host:port", "'offline_after_s' must be a positive",
                       "key must be a bare pi_id", "missing 'slot'", "missing 'serial'"):
            self.assertIn(needle, r.stdout)
        good = ShimProc(self.tmp, 7300, self.broker.port)
        r = subprocess.run([sys.executable, "-m", "uii_analyzer.mqtt_shim", "--check"],
                           cwd=ROOT, env=dict(os.environ, UII_SHIM_CONFIG=good.config,
                                              PYTHONPATH=ROOT),
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("ok: unit nh4mod -> slot-1 (NH4)", r.stdout)

    # -- the failure modes -------------------------------------------------------

    def test_unit_silence_says_bye_and_return_readopts_hands_off(self):
        self.start_shim(offline_after_s=3)
        self.start_unit()
        self.adopted()
        self.unit.kill(); self.unit.wait(timeout=3); self.unit = None
        wait_for(lambda: self.bench.state_of("nh4mod") == "REMOVED", timeout=15,
                 what="REMOVED after silence")
        self.assertIn("silent on MQTT 3s", self.shim.log())
        self.start_unit()                                   # the unit comes back
        self.adopted(timeout=20)
        self.assertGreaterEqual(self.shim.log().count("adopted as"), 2)

    def test_deaf_unit_is_nacked_after_ack_timeout(self):
        self.start_shim(ack_timeout_s=2)
        self.start_unit(FAKE_DEAF=1)                        # heartbeats, no answers
        self.adopted()
        st = self.done(self.cmd("take_control"), timeout=15)
        self.assertEqual(st["result"]["data"]["status"], "rejected")
        self.assertIn("did not respond within 2s", st["result"]["data"]["reason"])

    def test_broker_restart_reconnects_and_commands_work_after(self):
        self.start_shim()
        self.start_unit()
        self.adopted()
        self.broker.stop()
        wait_for(lambda: "lost 127.0.0.1" in self.shim.log(), timeout=15,
                 what="shim notices the broker is gone")
        self.broker.start()
        wait_for(lambda: self.shim.log().count("connected 127.0.0.1") >= 2,
                 timeout=30, what="shim reconnected")
        self.adopted()                                      # still / again operational
        st = self.done(self.cmd("take_control"))
        self.assertEqual(st["result"]["data"]["status"], "succeeded")

    def test_half_open_broker_is_detected_within_keepalive(self):
        self.start_shim(keepalive_s=2)                      # read timeout 3 s
        self.start_unit(FAKE_KEEPALIVE_S=2)
        self.adopted()
        self.broker.freeze()
        t0 = time.time()
        wait_for(lambda: "no traffic for 3s" in self.shim.log(), timeout=15,
                 what="half-open socket detected")
        self.assertLess(time.time() - t0, 10)
        self.broker.thaw()
        wait_for(lambda: self.shim.log().count("connected 127.0.0.1") >= 2,
                 timeout=40, what="shim reconnected after thaw")
        self.adopted(timeout=40)

    def test_hub_restart_readopts_without_touching_the_shim(self):
        self.start_shim()
        self.start_unit()
        self.adopted()
        sb, api = self.bench.hub.sb_port, self.bench.hub.api_port
        self.bench.hub.stop()
        wait_for(lambda: "hub connection lost" in self.shim.log(), timeout=15,
                 what="shim notices the hub is gone")
        from uii.hub.config import HubConfig
        from uii.hub.main import Hub
        self.bench.hub = Hub(HubConfig(self.bench.cfg_path), sb_port=sb, api_port=api,
                             speed=self.bench.speed).start()
        self.adopted(timeout=30)
        self.assertGreaterEqual(self.shim.log().count("adopted as"), 2)

    def test_unit_clock_skew_rides_in_health_and_is_logged(self):
        self.start_shim()
        self.start_unit(FAKE_CLOCK_OFFSET_S=-3600)          # an hour behind
        self.adopted()

        def skew():
            items = get(self.bench.base,
                        "/v1/modules/nh4mod/history?kind=health&since=0")["items"]
            for env in reversed(items):
                data = env.get("data") or {}
                if "clock_skew_s" in data:
                    return data["clock_skew_s"]
            return None
        value = wait_for(skew, timeout=20, what="clock_skew_s in health")
        self.assertAlmostEqual(value, 3600, delta=60)
        self.assertIn("clock skew +36", self.shim.log())

    def test_doctor_says_heard_versus_configured(self):
        from uii_analyzer.doctor import collect, summarize
        self.start_shim(units={"nh4mod-01": {"slot": "slot-1", "analyte": "NH4",
                                             "serial": "X"}})   # the field trap
        self.start_unit()                                        # publishes as nh4mod
        time.sleep(2)
        checks = collect(hub_url=self.bench.base, token=None,
                         config_path_=self.shim.config, listen_s=2)
        by_id = {c["id"]: c for c in checks}
        self.assertEqual(by_id["config"]["status"], "PASS")
        self.assertEqual(by_id["hub"]["status"], "PASS", by_id["hub"])
        self.assertEqual(by_id["broker"]["status"], "PASS", by_id["broker"])
        self.assertEqual(by_id["units"]["status"], "FAIL")
        self.assertIn("configured 'nh4mod-01' never heard", by_id["units"]["detail"])
        self.assertIn("heard instead: nh4mod", by_id["units"]["detail"])
        self.assertEqual(by_id["adopted"]["status"], "FAIL")
        self.assertEqual(summarize(checks)[1], 2)

        # and the fixed config passes
        self.shim.stop()
        self.start_shim()
        self.adopted()
        checks = collect(hub_url=self.bench.base, token=None,
                         config_path_=self.shim.config, listen_s=2)
        by_id = {c["id"]: c for c in checks}
        self.assertEqual(by_id["units"]["status"], "PASS", by_id["units"])
        self.assertEqual(by_id["adopted"]["status"], "PASS", by_id["adopted"])
        self.assertIn("nh4mod OPERATIONAL in slot-1", by_id["adopted"]["detail"])


if __name__ == "__main__":
    unittest.main()
