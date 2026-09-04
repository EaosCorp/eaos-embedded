"""The vision profile end to end: campod (sim) -> hub -> foam_coverage.

Proves both data shapes and the class-blind reuse:
  * telemetry  — streamed frames become foam_coverage without a command
  * on demand  — a capture command returns a frame and derives foam with lineage
  * permitted-use gating — a low-quality frame reports "none"
  * adoption/swap and evidence come free from the universal core
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

from tests.helpers import Bench, ROOT, post, wait_for

VIS_ROLES = {
    "slot-1": {"role": "aeration-foam-cam", "cadence_s": 0.05, "control_ok": False},
}


def spawn_campod(bench, module_id="campod-1", slot="slot-1", serial=None,
                 coverage=25.0, foam="nuisance_white", quality=1.0):
    frames_dir = os.path.join(bench.tmp, "data", "frames")
    env = dict(os.environ,
               UII_HUB=f"127.0.0.1:{bench.hub.sb_port}", UII_SIM="1",
               UII_SPEED=str(bench.speed),
               UII_MODULE_ID=module_id, UII_SLOT=slot, UII_TYPE="vision-cam",
               UII_SERIAL=serial or f"SN-{module_id.upper()}",
               UII_FRAMES_DIR=frames_dir, UII_CADENCE_S="0.05",
               UII_SIM_COVERAGE=str(coverage), UII_SIM_FOAM=foam,
               UII_SIM_QUALITY=str(quality), PYTHONPATH=ROOT)
    p = subprocess.Popen([sys.executable, "-m", "uii_vision.campod"],
                         cwd=ROOT, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    bench.procs.append(p)
    return p


class VisionProfileTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("UII_FRAMES_DIR", None)   # hub uses <data_dir>/frames
        self.bench = Bench(roles=VIS_ROLES, allowed=["vision-cam"],
                           extensions=["vision"])

    def tearDown(self):
        for p in self.bench.procs:
            p.terminate()
        for p in self.bench.procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        try:
            self.bench.hub.stop()
        except Exception:
            pass

    def foam_obs(self, module_id="campod-1", **kw):
        return self.bench.hub.store.query(kind="observation", module=module_id,
                                          channel="foam_coverage", **kw)

    def test_adopt_and_telemetry_stream(self):
        """Streamed frames become foam_coverage with no command in the loop."""
        spawn_campod(self.bench, coverage=25.0)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        obs = wait_for(lambda: self.foam_obs() or None, what="foam_coverage stream")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        val = latest["data"]["value"]
        self.assertTrue(20 <= val <= 30, f"coverage {val} not ~25")
        self.assertEqual(latest["quality"]["permitted_use"], "reporting")
        # telemetry frames carry no command_id in their lineage
        self.assertIsNone((latest.get("trace") or {}).get("command_id"))
        self.assertEqual(latest["data"]["foam_type"], "nuisance_white")

    def test_on_demand_capture_has_lineage(self):
        """A capture command grabs a frame and derives foam tied to the command."""
        spawn_campod(self.bench, coverage=50.0)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        status, body = post(self.bench.base, "/v1/commands",
                            {"module": "campod-1", "type": "capture"})
        self.assertEqual(status, 202, body)
        cmd_id = body["command_id"]
        cmd_obs = wait_for(lambda: self.foam_obs(command_id=cmd_id) or None,
                           what="foam from capture command")
        o = cmd_obs[0]
        self.assertEqual(o["trace"]["command_id"], cmd_id)
        self.assertTrue(45 <= o["data"]["value"] <= 55)
        # lineage: the derived foam points back at a real frame envelope
        lin = self.bench.hub.store.lineage(o["id"])
        self.assertTrue(lin["upstream"], "foam observation has no upstream frame")

    def test_frame_routes_head_and_caching(self):
        """GET/HEAD /v1/modules/<id>/frame: same headers, no body on HEAD,
        latest is no-store (a browser tab must refresh), by-hash is immutable,
        and X-Frame-Hash / X-Frame-Time identify the frame served."""
        spawn_campod(self.bench, coverage=25.0)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        wait_for(lambda: self.foam_obs() or None, what="first frame stored")
        base = self.bench.base

        def req(method, path):
            r = urllib.request.Request(base + path, method=method)
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, dict(resp.headers), resp.read()

        st, hdr, body = req("GET", "/v1/modules/campod-1/frame")
        self.assertEqual(st, 200)
        self.assertEqual(hdr["Content-Type"], "image/jpeg")
        self.assertEqual(hdr["Cache-Control"], "no-store")
        self.assertTrue(body.startswith(b"\xff\xd8"), "not a JPEG")
        digest = hdr["X-Frame-Hash"]
        self.assertRegex(digest, r"^[0-9a-f]{64}$")
        self.assertIn("T", hdr["X-Frame-Time"])

        st, hhdr, hbody = req("HEAD", "/v1/modules/campod-1/frame")
        self.assertEqual(st, 200)
        self.assertEqual(hbody, b"", "HEAD must not send a body")
        self.assertEqual(hhdr["Content-Type"], "image/jpeg")
        self.assertEqual(hhdr["Cache-Control"], "no-store")
        self.assertIn("X-Frame-Hash", hhdr)

        st, bh, bbody = req("GET", f"/v1/frames/{digest}?full=1")
        self.assertEqual(st, 200)
        self.assertIn("immutable", bh["Cache-Control"])
        self.assertEqual(bh["X-Frame-Hash"], digest)

        # HEAD on a JSON route and on a 404 behave the same as GET, sans body
        st, jh, jb = req("HEAD", "/v1/modules")
        self.assertEqual((st, jb), (200, b""))
        self.assertEqual(jh["Content-Type"], "application/json")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            req("HEAD", "/v1/nope")
        self.assertEqual(cm.exception.code, 404)
        self.assertEqual(cm.exception.read(), b"")

    def test_low_quality_gates_to_none(self):
        """A dark/blurry frame is not fit for reporting."""
        spawn_campod(self.bench, coverage=30.0, quality=0.15)
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        wait_for(lambda: self.foam_obs() or None, what="foam_coverage")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        self.assertEqual(latest["quality"]["permitted_use"], "none")
        self.assertIn(latest["quality"]["status"], ("bad",))

    def test_brown_foam_type(self):
        """Biological (Nocardia-style) foam classifies distinctly from nuisance."""
        spawn_campod(self.bench, coverage=45.0, foam="biological_brown")
        wait_for(lambda: self.bench.state_of("campod-1") == "OPERATIONAL",
                 what="campod adopted")
        wait_for(lambda: self.foam_obs() or None, what="foam_coverage")
        latest = self.bench.hub.store.latest_observation("campod-1", "foam_coverage")
        self.assertEqual(latest["data"]["foam_type"], "biological_brown")


class PolygonRoiTest(unittest.TestCase):
    """Commissioned outlines are polygons (one per basin/channel), not a box."""

    def setUp(self):
        import numpy as np
        self.np = np

    def test_polygon_roi_matches_rect_for_a_rectangle(self):
        from uii_vision.foam_cv import polygon_roi, rect_roi
        shape = (108, 192)
        rect = rect_roi(shape, 0.25, 0.5, 0.75, 1.0)
        poly = polygon_roi(shape, [[[0.25, 0.5], [0.75, 0.5], [0.75, 1.0], [0.25, 1.0]]])
        self.assertGreater((rect == poly).mean(), 0.99)

    def test_polygon_roi_is_concave_aware_and_unions(self):
        from uii_vision.foam_cv import polygon_roi
        shape = (100, 100)
        # L-shape: the notch (top-right quadrant) must be outside
        l_shape = [[0, 0], [0.5, 0], [0.5, 0.5], [1, 0.5], [1, 1], [0, 1]]
        m = polygon_roi(shape, {"a": l_shape})
        self.assertTrue(m[90, 10]); self.assertTrue(m[90, 90]); self.assertTrue(m[10, 10])
        self.assertFalse(m[10, 90])
        two = polygon_roi(shape, {"a": [[0, 0], [0.4, 0], [0.4, 0.4], [0, 0.4]],
                                  "b": [[0.6, 0.6], [1, 0.6], [1, 1], [0.6, 1]]})
        self.assertTrue(two[10, 10]); self.assertTrue(two[90, 90]); self.assertFalse(two[50, 50])
        self.assertEqual(int(polygon_roi(shape, {}).sum()), 0)

    def test_build_roi_and_regions(self):
        from uii_vision.foam_cv import build_roi, region_masks, detect
        np = self.np
        shape = (60, 80)
        spec = {"polygons": {"west": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]],
                             "east": [[0.5, 0], [1, 0], [1, 1], [0.5, 1]]}}
        self.assertIsNone(build_roi(shape, None))
        self.assertEqual(int(build_roi(shape, (0, 0, 1, 1)).sum()), 60 * 80)
        self.assertEqual(int(build_roi(shape, {"x0": 0, "y0": 0, "x1": 1, "y1": 0.5}).sum()), 30 * 80)
        union = build_roi(shape, spec)
        self.assertEqual(int(union.sum()), 60 * 80)
        regs = region_masks(shape, spec)
        self.assertEqual(set(regs), {"west", "east"})
        self.assertEqual(region_masks(shape, (0, 0, 1, 1)), {})
        # west half white foam, east half dark water -> per-region split
        rgb = np.zeros((60, 80, 3), dtype=np.uint8); rgb[:, :, :] = 40
        rgb[:, :40, :] = 245
        self.assertGreater(detect(rgb, roi=regs["west"])["coverage_pct"], 90)
        self.assertLess(detect(rgb, roi=regs["east"])["coverage_pct"], 5)

    def test_roi_spec_normalises_all_shapes(self):
        from uii_vision.interpreter import _roi_spec
        self.assertIsNone(_roi_spec(None)); self.assertIsNone(_roi_spec({}))
        self.assertEqual(_roi_spec({"x0": 0, "y0": 0.2, "x1": 1, "y1": 1}), (0, 0.2, 1, 1))
        self.assertEqual(_roi_spec([0, 0.2, 1, 1]), (0, 0.2, 1, 1))
        p = _roi_spec({"polygons": [[[0, 0], [1, 0], [1, 1]]]})
        self.assertEqual(list(p["polygons"]), ["region-1"])
        p = _roi_spec({"polygons": {"b1": [[0, 0], [1, 0], [1, 1]]}})
        self.assertEqual(p["polygons"]["b1"][2], [1.0, 1.0])

    def test_commissioned_roi_files_parse(self):
        import glob, json
        from uii_vision.foam_cv import polygon_roi
        files = glob.glob(os.path.join(os.path.dirname(__file__), "..", "uii_vision", "config", "roi", "*.json"))
        self.assertTrue(files)
        for f in files:
            spec = json.load(open(f))
            m = polygon_roi((108, 192), spec["polygons"])
            self.assertGreater(int(m.sum()), 500, f)
            for name, poly in spec["polygons"].items():
                self.assertGreaterEqual(len(poly), 3, name)
                self.assertTrue(all(0 <= x <= 1 and 0 <= y <= 1 for x, y in poly), name)


class FoamRegionModelTest(unittest.TestCase):
    """The learned per-region model loads from a dir and degrades cleanly."""

    def test_unavailable_without_files(self):
        from uii_vision.foam_region import FoamRegionModel
        m = FoamRegionModel("/nonexistent/dir")
        self.assertFalse(m.available); self.assertIn("Error", m.error)
        self.assertEqual(m.predict(None, {"a": [[0, 0], [1, 0], [1, 1]]}), {})

    def test_predict_with_trained_model_if_present(self):
        import numpy as np
        from uii_vision.foam_region import FoamRegionModel
        d = os.path.expanduser("~/eaos/eaos-clients/dc-water/projects/dcw-001-blue-plains-ftt/foam-cv/models/foam-region-v0.1")
        if not os.path.exists(os.path.join(d, "model.onnx")):
            self.skipTest("no trained model on this machine")
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime not installed")
        m = FoamRegionModel(d); self.assertTrue(m.available, m.error)
        rgb = np.zeros((360, 640, 3), np.uint8); rgb[:, :, :] = 235          # bright = foam-like
        out = m.predict(rgb, {"west": [[0.1, 0.5], [0.5, 0.5], [0.5, 0.9], [0.1, 0.9]],
                              "east": [[0.55, 0.5], [0.9, 0.5], [0.9, 0.9], [0.55, 0.9]]})
        self.assertEqual(set(out), {"west", "east"})
        for r in out.values():
            self.assertIn(r["bucket"], range(5)); self.assertAlmostEqual(sum(r["bucket_probs"]), 1.0, places=2)
            self.assertIn(r["colour"], ("white", "brown", "mixed"))


class BfpDoseTest(unittest.TestCase):
    """The whole-frame dose classifier loads, predicts, and degrades cleanly."""

    def test_unavailable_without_files(self):
        from uii_vision.bfp_dose import BfpDoseModel
        m = BfpDoseModel("/nonexistent/dir")
        self.assertFalse(m.available); self.assertIsNone(m.predict(None))

    def test_predict_with_exported_model_if_present(self):
        import numpy as np
        d = os.path.expanduser("~/eaos/eaos-clients/dc-water/computer_vision/dcwater-cv/export/bfp-dose-v1")
        if not os.path.exists(os.path.join(d, "model.onnx")):
            self.skipTest("no exported model on this machine")
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime not installed")
        from uii_vision.bfp_dose import BfpDoseModel
        m = BfpDoseModel(d); self.assertTrue(m.available, m.error)
        out = m.predict(np.random.randint(0, 255, (720, 1280, 3), np.uint8))
        self.assertIn(out["dose_class"], ("Good", "Overdose", "Underdose"))
        self.assertAlmostEqual(sum(out["probs"].values()), 1.0, places=2)


    def test_corn_decode_and_ensemble(self):
        import json, tempfile, numpy as np
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            self.skipTest("onnxruntime not installed")
        from uii_vision.bfp_dose import BfpDoseModel, BfpDoseEnsemble
        # tiny identity-ish onnx: 2-logit output via matmul on mean pixel is overkill;
        # instead test decode math directly through a stub model object.
        m = BfpDoseModel.__new__(BfpDoseModel)
        m.meta = {"classes": ["Good", "Overdose", "Underdose"], "input": {"size": 2, "mean": [0, 0, 0], "std": [1, 1, 1]},
                  "head": {"type": "corn", "rank_to_class": ["Underdose", "Good", "Overdose"]}}
        class Sess:
            def run(self, _, feeds): return [np.array([[+5.0, -5.0]], dtype=np.float32)]
        m.sess = Sess(); m.error = None; m.dir = "stub"
        out = m.predict(np.zeros((4, 4, 3), np.uint8))
        self.assertEqual(out["dose_class"], "Good")           # spec rank ordering test
        Sess.run = lambda self, _, feeds: [np.array([[-5.0, -5.0]], dtype=np.float32)]
        self.assertEqual(m.predict(np.zeros((4, 4, 3), np.uint8))["dose_class"], "Underdose")
        Sess.run = lambda self, _, feeds: [np.array([[+5.0, +5.0]], dtype=np.float32)]
        self.assertEqual(m.predict(np.zeros((4, 4, 3), np.uint8))["dose_class"], "Overdose")
        e = BfpDoseEnsemble(tempfile.mkdtemp())
        self.assertFalse(e.available); self.assertIsNone(e.predict(np.zeros((4, 4, 3), np.uint8)))

    def test_gopro_source_health_gate(self):
        """A GoPro with a dead SD card raises a health error, never returns a frame."""
        import http.server, threading, json as _json
        from uii_vision.framesource import GoProSnapshotSource
        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = _json.dumps({"status": {"33": 2, "54": 0, "111": 1, "117": 0}}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
            def log_message(self, *a): pass
        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            src = GoProSnapshotSource(host="127.0.0.1", port=srv.server_port)
            with self.assertRaises(RuntimeError) as cm:
                src.grab()
            self.assertIn("gopro-health", str(cm.exception))
        finally:
            srv.shutdown()

    def test_make_source_kind_switch(self):
        from uii_vision.framesource import make_source, GoProSnapshotSource, HTTPSnapshotSource, SimFrameSource
        self.assertIsInstance(make_source({"CAMERA_KIND": "gopro"}), GoProSnapshotSource)
        self.assertIsInstance(make_source({}), HTTPSnapshotSource)
        self.assertIsInstance(make_source({"UII_SIM": "1"}), SimFrameSource)


if __name__ == "__main__":
    unittest.main()
