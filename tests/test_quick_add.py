import re
import json
import tempfile
import unittest
from pathlib import Path

import app.main as main


class QuickAddTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/quick-add")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        for name in ("Seeed Studio XIAO ESP32-C3", "ESP32-S3 DevKit"):
            self.client.post("/items/new", data=dict(name=name, quantity="2", unit="pcs", location_id=self.location))

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def test_fuzzy_variants_and_home(self):
        matches = self.client.get("/api/search?q=ESP32").json
        self.assertEqual(len(matches), 2)
        for query in ("seed studio esp32 c3", "ESP-32 C3", "seeeed xiao"):
            matches = self.client.get("/api/search", query_string={"q": query}).json
            self.assertEqual(matches[0]["name"], "Seeed Studio XIAO ESP32-C3")
        self.assertEqual(self.client.get("/api/search?q=esp32%20c6").json, [])
        html = self.client.get("/quick-add?q=ESP32-C6").get_data(as_text=True)
        self.assertIn("4L 01", html)
        self.assertIn("No matching stock yet", html)
        self.assertNotIn("4L 01", self.client.get("/quick-add?q=unrecognisablewidget").get_data(as_text=True))

    def test_receipt_is_atomic_and_replay_safe(self):
        html = self.client.get("/quick-add?q=ESP32&item=1").get_data(as_text=True)
        fields = {name: re.search('name="' + name + '" value="([^"]+)"', html)[1] for name in ("token", "csrf")}
        fields.update(item="1", q="ESP32", quantity="3")
        self.assertEqual(self.client.post("/quick-add", data=fields).status_code, 302)
        self.assertEqual(self.client.post("/quick-add", data=fields).status_code, 302)
        with main.app.app_context():
            self.assertEqual(main.db().execute("SELECT quantity FROM items WHERE id=1").fetchone()[0], 5)
            self.assertEqual(main.db().execute("SELECT count(*) FROM movements WHERE reason='Stock received'").fetchone()[0], 1)
        for invalid in ("-1", "0", "nan", "inf", "abc"):
            fields["quantity"] = invalid
            self.assertIn(b"quantity greater than zero", self.client.post("/quick-add", data=fields).data)
        fields["csrf"] = "bad"
        self.assertEqual(self.client.post("/quick-add", data=fields).status_code, 400)

    def test_new_variant_prefilled_and_invalid_location_rejected(self):
        html = self.client.get("/items/new", query_string={"name": "ESP32-C6", "location": self.location}).get_data(as_text=True)
        self.assertIn('value="ESP32-C6"', html)
        self.assertIn(f'value="{self.location}" data-code="4L 01" selected', html)
        self.assertIn(b"choose a storage location", self.client.post("/items/new", data=dict(name="Invalid", quantity="1", location_id="999999")).data)

    def test_item_guide_classifies_parts_and_suggests_common_boards(self):
        capacitor = self.client.get("/api/item-guide", query_string={"q": "100nF ceramic capacitor"}).json
        self.assertEqual(capacitor["family"], "capacitor")
        self.assertIn("Capacitance", [field[1] for field in capacitor["fields"]])

        esp32 = self.client.get("/api/item-guide", query_string={"q": "ESP32"}).json
        self.assertEqual(esp32["family"], "microcontroller")
        self.assertTrue(any("XIAO ESP32" in item["name"] for item in esp32["suggestions"]))

    def test_catalogue_suggestion_prefills_new_item(self):
        html = self.client.get("/items/new", query_string={"catalogue": "0"}).get_data(as_text=True)
        self.assertIn('value="Seeed Studio XIAO ESP32-C3"', html)
        self.assertIn('value="ESP32-C3"', html)
        self.assertIn(f'value="{self.location}" data-code="4L 01" selected', html)

    def test_adaptive_attributes_are_saved_and_displayed(self):
        response = self.client.post("/items/new", data={
            "name": "100 nF Ceramic capacitor",
            "family": "capacitor",
            "quantity": "25",
            "unit": "pcs",
            "location_id": self.location,
            "attr_value": "100",
            "attr_value_unit": "nF",
            "attr_voltage": "50 V",
            "attr_capacitor_type": "Ceramic",
            "attr_package": "0805",
        })
        self.assertEqual(response.status_code, 302)
        with main.app.app_context():
            item = main.db().execute("SELECT * FROM items WHERE name='100 nF Ceramic capacitor'").fetchone()
            self.assertEqual(item["family"], "capacitor")
            self.assertEqual(json.loads(item["attributes"])["package"], "0805")
        html = self.client.get(response.headers["Location"]).get_data(as_text=True)
        self.assertIn("Capacitor", html)
        self.assertIn("Package / size", html)
        self.assertIn("0805", html)

    def test_inventory_csv_report_is_downloadable(self):
        response = self.client.get("/reports/inventory.csv")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertIn(b"Component,Manufacturer,Part number", response.data)


if __name__ == "__main__":
    unittest.main()
