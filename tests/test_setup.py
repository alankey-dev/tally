import os
import json
import tempfile
import unittest
from pathlib import Path

import app.main as main


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        self.client = main.app.test_client()

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def location_codes(self):
        with main.app.app_context():
            main.init_db()
            return [row[0] for row in main.db().execute("SELECT code FROM locations ORDER BY code")]

    def test_new_install_starts_empty(self):
        main.LAYOUT = ""
        self.assertEqual(self.location_codes(), [])
        self.assertIn("No storage yet", self.client.get("/locations").get_data(as_text=True))
        self.assertIn("storage location</a> first", self.client.get("/items/new").get_data(as_text=True))

    def test_bundled_example_layout(self):
        main.LAYOUT = "example"
        self.assertEqual(len(self.location_codes()), 141)
        self.assertIn("Connectors &amp; wiring", self.client.get("/locations").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/item-guide?q=resistor").json["home"], "C1 S01")

    def test_layout_from_a_file(self):
        path = Path(self.directory.name) / "garage.json"
        path.write_text(json.dumps({
            "groups": {"SHELF": "Garage shelving"},
            "family_homes": {"cable": "SHELF 2"},
            "locations": [{"code": "SHELF 1", "label": "Motors", "keywords": "stepper servo"}, {"code": "SHELF 2", "label": "Leads"}],
        }))
        main.LAYOUT = str(path)
        self.assertEqual(self.location_codes(), ["SHELF 1", "SHELF 2"])
        self.assertIn("Garage shelving", self.client.get("/locations").get_data(as_text=True))
        self.assertIn("SHELF 1", self.client.get("/quick-add?q=nema17 stepper").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/item-guide?q=hdmi cable").json["home"], "SHELF 2")

    def test_generated_secret_key_is_kept(self):
        previous = os.environ.pop("SECRET_KEY", None)
        try:
            first = main.secret_key()
            self.assertEqual(len(first), 64)
            self.assertEqual(main.secret_key(), first)
            self.assertEqual((main.DB_PATH.parent / "secret_key").stat().st_mode & 0o777, 0o600)
        finally:
            if previous is not None:
                os.environ["SECRET_KEY"] = previous


if __name__ == "__main__":
    unittest.main()
