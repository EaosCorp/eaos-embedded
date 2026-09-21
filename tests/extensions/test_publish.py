"""publish extension: what crosses the cellular link, and what does not.

The rule under test is the budget one. The readings move every cycle and the picture often does
not, so the readings go every cycle and a FRAME goes only when its hash changed since the last one
the plane accepted. A failed post must not mark a frame as sent, or the next unchanged cycle would
skip a picture the plane never got.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from extensions.publish.publish import Publisher

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 128 + b"\xff\xd9"


class _Store:
    def __init__(self, obs=None):
        self.obs = obs or {}
        self.events = []

    def latest_observation(self, module, channel):
        return self.obs.get((module, channel))

    def append(self, *a, **k):
        self.events.append((a, k))


class _Hub:
    def __init__(self, store):
        self.store = store
        self.southbound = type("S", (), {"sessions": {}})()
        self.config = type("C", (), {"data_dir": "/tmp/nope", "raw": {}})()


def _hub(frame_hash="h1", pct=32.4):
    obs = {
        ("campod-01", "frames"): {"time": "2026-09-15T18:00:00Z", "data": {"frame_hash": frame_hash}},
        ("campod-01", "foam_coverage"): {"time": "2026-09-15T18:05:00Z",
                                         "data": {"value": pct, "foam_type": "nuisance_white"},
                                         "quality": {"permitted_use": "reporting"}},
        ("campod-01", "image_quality"): {"data": {"value": 0.88}},
    }
    return _Hub(_Store(obs))


def _publisher(hub, posts, token="t0k"):
    tmp = Path(tempfile.mkdtemp()) / "token"
    tmp.write_text(token + "\n")
    p = Publisher(hub, {"url": "https://plane.example/api/frames/ingest", "every_s": 900,
                        "token_file": str(tmp), "modules": {"campod-01": "campod-01"}})
    p._blobs = type("B", (), {"get": staticmethod(lambda d: JPEG if d else None)})()
    p._post = lambda payload, tok: (posts.append((payload, tok)), {"ok": True})[1]
    return p


class TestPublish(unittest.TestCase):
    def test_the_first_tick_sends_the_frame_and_the_readings(self):
        posts = []
        p = _publisher(_hub(), posts)
        self.assertEqual(p.tick(), 1)
        payload, tok = posts[0]
        self.assertEqual(tok, "t0k")                       # read from the file, stripped
        self.assertEqual(payload["camera"], "campod-01")
        self.assertIsNotNone(payload["image_b64"])
        self.assertEqual(payload["frame_hash"], "h1")
        self.assertEqual(payload["readings"]["foam_coverage_pct"], 32.4)
        self.assertEqual(payload["readings"]["foam_type"], "nuisance_white")
        self.assertEqual(payload["readings"]["image_quality"], 0.88)
        # the measurement's own time, not the frame's: a watch ages on the measurement
        self.assertEqual(payload["captured_at"], "2026-09-15T18:05:00Z")

    def test_the_regions_travel_with_the_readings(self):
        """The interpreter scores every named polygon of the ROI and keeps them on the foam observation
        (`data.regions`); the plane joins them to assets through cameras.yaml. Only numbers, a class and
        the region model's label/score/bucket go over the link -- never a model's vector."""
        hub = _hub()
        hub.store.obs[("campod-01", "foam_coverage")]["data"]["regions"] = {
            "basin-west": {"coverage_pct": 6.1, "foam_type": "nuisance_white", "confidence": 0.7,
                           "model": {"label": "light", "score": 0.61, "bucket": "low", "vector": [0.1, 0.2]}},
            "basin-east": {"coverage_pct": 2.8, "foam_type": "none"},
            "junk": "not a mapping"}
        posts = []
        p = _publisher(hub, posts)
        p.tick()
        regions = posts[0][0]["regions"]
        self.assertEqual(set(regions), {"basin-west", "basin-east"})
        self.assertEqual(regions["basin-west"]["model"], {"label": "light", "score": 0.61, "bucket": "low"})
        self.assertEqual(regions["basin-east"], {"coverage_pct": 2.8, "foam_type": "none"})
        self.assertEqual(posts[0][0]["readings"]["foam_coverage_pct"], 32.4)

    def test_a_box_with_no_regions_sends_an_empty_map(self):
        posts = []
        _publisher(_hub(), posts).tick()
        self.assertEqual(posts[0][0]["regions"], {})

    def test_an_unchanged_picture_is_not_sent_again_but_the_numbers_are(self):
        posts = []
        p = _publisher(_hub(), posts)
        p.tick()
        p.hub.store.obs[("campod-01", "foam_coverage")]["data"]["value"] = 57.1
        self.assertEqual(p.tick(), 1)
        payload, _ = posts[1]
        self.assertIsNone(payload["image_b64"])            # ~150 KB not spent
        self.assertEqual(payload["readings"]["foam_coverage_pct"], 57.1)
        self.assertEqual(payload["frame_hash"], "h1")      # the plane carries its stored frame forward

    def test_a_new_picture_is_sent(self):
        posts = []
        p = _publisher(_hub(), posts)
        p.tick()
        p.hub.store.obs[("campod-01", "frames")]["data"]["frame_hash"] = "h2"
        p.tick()
        self.assertIsNotNone(posts[1][0]["image_b64"])
        self.assertEqual(posts[1][0]["frame_hash"], "h2")

    def test_a_post_that_failed_does_not_count_as_sent(self):
        posts = []
        p = _publisher(_hub(), posts)

        def boom(payload, tok):
            raise OSError("no route")

        p._post = boom
        self.assertEqual(p.tick(), 0)
        p._post = lambda payload, tok: (posts.append((payload, tok)), {})[1]
        p.tick()
        self.assertIsNotNone(posts[0][0]["image_b64"])     # the frame is still owed and still sent

    def test_no_token_sends_nothing_and_says_so(self):
        posts = []
        p = _publisher(_hub(), posts)
        p.token_file = "/nonexistent/token"
        p.token_env = "EAOS_TEST_NO_SUCH_TOKEN"
        self.assertEqual(p.tick(), 0)
        self.assertEqual(posts, [])
        self.assertTrue(any("no ingest token" in str(e) for e in p.store.events))

    def test_a_module_with_nothing_to_say_is_skipped(self):
        posts = []
        hub = _Hub(_Store({}))
        p = _publisher(hub, posts)
        self.assertEqual(p.tick(), 0)

    def test_setup_without_a_url_registers_nothing(self):
        import extensions.publish as ext
        hub = _hub()
        registered = []
        hub.add_service = registered.append
        hub.config.raw = {}
        ext.setup(hub)
        self.assertEqual(registered, [])
        hub.config.raw = {"publish": {"url": "https://plane.example/api/frames/ingest"}}
        ext.setup(hub)
        self.assertEqual(len(registered), 1)


if __name__ == "__main__":
    unittest.main()
