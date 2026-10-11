import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.main as main
from app import jev


class JevTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def enable(self):
        self.client.post("/settings/jev", data={"api_key": "secret-key", "enabled": "1"})

    def test_needs_key(self):
        response = self.client.post("/api/classify", json={"name": "NE555P"})
        self.assertEqual(response.status_code, 400)

    def test_key_is_saved_and_not_shown(self):
        self.enable()
        page = self.client.get("/settings/jev").get_data(as_text=True)
        self.assertNotIn("secret-key", page)
        self.assertIn("Saved. Leave blank to keep it", page)

    def test_classify_picks_family_and_location(self):
        self.enable()
        with mock.patch.object(jev, "classify", return_value={"family": ("resistor", 0.9), "location": ("C1 S01", 0.8)}) as call:
            response = self.client.post("/api/classify", json={"name": "10k resistor"})
        data = response.get_json()
        self.assertEqual((data["family"], data["location"]), ("resistor", "C1 S01"))
        self.assertTrue(data["location_id"])
        sent_locations = dict(call.call_args.args[3])
        self.assertIn("C1 S01", sent_locations)

    def test_jev_error_is_reported(self):
        self.enable()
        with mock.patch.object(jev, "classify", side_effect=jev.JevError("Jev rejected the API key.")):
            response = self.client.post("/api/classify", json={"name": "x"})
        self.assertEqual(response.status_code, 502)
        self.assertIn("rejected", response.get_json()["error"])

    def test_request_shape(self):
        reply = b'{"answers": {"family": {"choice": "resistor", "confidence": 0.7}, "location": {"choice": "A", "confidence": 0.6}}}'
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self, n): return reply
        with mock.patch("urllib.request.urlopen", return_value=Response()) as urlopen:
            result = jev.classify("k", "Name: x", {"resistor": "Resistor"}, [("A", "Drawer A")])
        sent = urlopen.call_args.args[0]
        self.assertEqual(sent.get_header("Authorization"), "Bearer k")
        self.assertEqual(result, {"family": ("resistor", 0.7), "location": ("A", 0.6)})

    def test_quick_add_offers_and_uses_jev(self):
        self.assertNotIn("Ask Jev", self.client.get("/quick-add?fragment=1&q=zzqx widget").get_data(as_text=True))
        self.enable()
        offer = self.client.get("/quick-add?fragment=1&q=zzqx widget").get_data(as_text=True)
        self.assertIn("Suggest family and home", offer)
        with mock.patch.object(jev, "classify", return_value={"family": ("resistor", 0.9), "location": ("C1 S01", 0.8)}) as call:
            answer = self.client.get("/quick-add?fragment=1&q=zzqx widget&jev=1").get_data(as_text=True)
        self.assertIn("Use this suggestion", answer)
        self.assertIn("family=resistor", answer)
        self.assertIn("Name: zzqx widget", call.call_args.args[1])
