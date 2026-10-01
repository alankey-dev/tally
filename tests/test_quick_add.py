import io
import re
import json
import tempfile
import unittest
from unittest import mock
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
        html = self.client.get("/quick-add?q=xiao%20esp32%20c3").get_data(as_text=True)
        catalogue_id = re.search(r'catalogue=(\d+)">\s*<strong>Seeed Studio XIAO ESP32-C3<', html)[1]
        html = self.client.get("/items/new", query_string={"catalogue": catalogue_id}).get_data(as_text=True)
        self.assertIn('value="Seeed Studio XIAO ESP32-C3"', html)
        self.assertIn('value="ESP32-C3"', html)
        self.assertIn(f'value="{self.location}" data-code="4L 01" selected', html)

    def test_catalogue_imports_from_file_and_url(self):
        csv_file = (io.BytesIO(b"name,family,manufacturer,part_number,model,interface\nBME280 breakout,sensor,Bosch,BME280,BME280,I2C\n"), "parts.csv")
        response = self.client.post("/settings/catalogue", data={"file": csv_file}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 302)
        suggestions = self.client.get("/api/item-guide", query_string={"q": "bme280"}).json["suggestions"]
        self.assertEqual(suggestions[0]["attributes"], {"model": "BME280", "interface": "I2C"})
        self.assertTrue(self.client.get("/api/item-guide", query_string={"q": "ESP32"}).json["suggestions"])

        payload = json.dumps({"items": [{"name": "bme280 BREAKOUT", "family": "sensor", "part_number": "BME280-B"}, {"name": "Unknown thing", "family": "spaceship"}]}).encode()
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(payload)) as urlopen:
            self.client.post("/settings/catalogue", data={"url": "https://example.com/parts.json", "replace": "on"})
        self.assertEqual(urlopen.call_args[0][0].full_url, "https://example.com/parts.json")
        exported = self.client.get("/settings/catalogue.json").json["items"]
        self.assertEqual([(item["name"], item["family"], item["part_number"]) for item in exported], [("bme280 BREAKOUT", "sensor", "BME280-B"), ("Unknown thing", "generic", "")])
        self.assertIn('value="https://example.com/parts.json"', self.client.get("/settings/catalogue").get_data(as_text=True))

        for bad in (b"[{\"family\": \"sensor\"}]", b"not,a,catalogue\n1,2,3", b"\xff\xfe"):
            response = self.client.post("/settings/catalogue", data={"file": (io.BytesIO(bad), "bad.json")}, content_type="multipart/form-data", follow_redirects=True)
            self.assertIn(b'class="flash error', response.data)
        self.assertEqual(len(self.client.get("/settings/catalogue.json").json["items"]), 2)

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


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/quick-add")

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def test_settings_list_providers_and_hide_saved_key(self):
        html = self.client.get("/settings/providers").get_data(as_text=True)
        self.assertIn("JLCPCB / LCSC", html)
        self.assertIn("Mouser", html)
        self.client.post("/settings/providers/mouser", data=dict(enabled="1", api_key="secret-key"))
        html = self.client.get("/settings/providers").get_data(as_text=True)
        self.assertNotIn("secret-key", html)
        self.client.post("/settings/providers/mouser", data=dict(enabled="1", api_key=""))
        with main.app.app_context():
            self.assertEqual(main.provider_configs()["mouser"]["api_key"], "secret-key")
        self.assertEqual(self.client.post("/settings/providers/nope").status_code, 404)

    def test_search_uses_enabled_providers_only(self):
        fake = [{"name": "NE555P", "family": "semiconductor", "manufacturer": "TI", "part_number": "NE555P", "description": "Timer", "url": "", "attributes": {"package": "DIP-8"}}]
        with mock.patch.dict(main.part_providers.PROVIDERS["jlcpcb"], {"search": lambda query, config: fake}), \
                mock.patch.dict(main.part_providers.PROVIDERS["mouser"], {"search": mock.Mock(side_effect=AssertionError)}):
            html = self.client.get("/quick-add?q=555+timer&providers=1").get_data(as_text=True)
            self.assertIn("NE555P", html)
            self.assertIn("attr_package=DIP-8", html)
            self.assertNotIn("Mouser", html.split("Search providers")[1].split("New variant")[0].replace("Mouser's", ""))
            self.assertIn("Search providers", self.client.get("/quick-add?q=555+timer").get_data(as_text=True))
            self.client.post("/settings/providers/jlcpcb", data={})
            self.assertNotIn("Search providers", self.client.get("/quick-add?q=555+timer").get_data(as_text=True))

    def test_provider_error_is_shown(self):
        def fail(query, config):
            raise main.part_providers.ProviderError("The provider could not be reached.")
        with mock.patch.dict(main.part_providers.PROVIDERS["jlcpcb"], {"search": fail}):
            html = self.client.get("/quick-add?q=555+timer&providers=1").get_data(as_text=True)
        self.assertIn("could not be reached", html)

    def test_new_item_prefilled_from_provider_result(self):
        html = self.client.get("/items/new?name=NE555P&manufacturer=TI&part_number=NE555P&attr_package=DIP-8").get_data(as_text=True)
        self.assertIn('value="TI"', html)
        self.assertIn("DIP-8", html)

    def test_mouser_parses_parts(self):
        body = {"SearchResults": {"Parts": [{"ManufacturerPartNumber": "RC0805FR-0710KL", "Manufacturer": "YAGEO", "Description": "Thick Film Resistors 10K ohm", "Category": "Resistors", "MouserPartNumber": "603-RC0805FR-0710KL", "ProductDetailUrl": "https://www.mouser.com/x"}]}}
        with mock.patch.object(main.part_providers, "_request", return_value=body):
            results = main.part_providers.search_mouser("10k", {"api_key": "k"})
        self.assertEqual((results[0]["manufacturer"], results[0]["family"], results[0]["attributes"]["mouser"]), ("YAGEO", "resistor", "603-RC0805FR-0710KL"))


class SettingsPageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def test_each_section_has_its_own_page(self):
        expected = {"": "Features", "catalogue": "Quick add catalogue", "providers": "Part search providers", "webhooks": "Destination URL", "security": "Download backup"}
        for section, text in expected.items():
            html = self.client.get(f"/settings/{section}".rstrip("/")).get_data(as_text=True)
            self.assertIn(text, html, section)
            for other, other_text in expected.items():
                if other != section:
                    self.assertNotIn(f"<h2>{other_text}</h2>", html)
        self.assertEqual(self.client.get("/settings/nope").status_code, 404)

    def test_saving_returns_to_the_same_section(self):
        self.assertTrue(self.client.post("/settings/providers/mouser", data={}).location.endswith("/settings/providers"))
        self.assertTrue(self.client.post("/settings/webhooks", data={}).location.endswith("/settings/webhooks"))
        self.assertTrue(self.client.post("/settings/features", data={}).location.endswith("/settings"))
