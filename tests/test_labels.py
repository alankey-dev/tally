import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import app.main as main


class LabelTests(unittest.TestCase):
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

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def location(self, code):
        return self.query("SELECT id FROM locations WHERE code=?", code)[0][0]

    def add_location(self, code):
        with main.app.app_context():
            main.db().execute("INSERT INTO locations(code, kind, label, notes, image_path, created_at) VALUES (?, 'custom storage', 'Test', '', '', '')", (code,))
            main.db().commit()
        return self.location(code)

    def add_item(self, name, location_id, quantity="5"):
        self.client.post("/items/new", data=dict(name=name, quantity=quantity, unit="pcs", location_id=location_id))
        return self.query("SELECT id FROM items WHERE name=?", name)[0][0]

    def pages(self, response):
        return len(re.findall(rb"/Type /Page\b", response.data))

    def codes(self, path, **params):
        with main.app.test_request_context(path, query_string=params):
            return [row["code"] for row in main.select_locations(params.get("group", ""), params.get("q", ""))]

    def test_location_labels_pdf(self):
        count = len(self.query("SELECT 1 FROM locations WHERE code LIKE 'C1 %' OR code='C1'"))
        response = self.client.get("/locations/labels.pdf?group=C1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))
        self.assertIn(b"/PrintScaling /None", response.data)
        self.assertEqual(self.pages(response), -(-count // 21))
        self.assertEqual(self.pages(self.client.get("/locations/labels.pdf?group=C1&skip=20")), -(-(count + 20) // 21))
        for params in ("preset=nope", "skip=-5", "skip=9999", "skip=abc", "preset=roll-62x29&outlines=1", "preset=a4-5x13"):
            self.assertEqual(self.client.get("/locations/labels.pdf?group=C1&" + params).status_code, 200, params)
        self.assertEqual(self.client.get("/locations/labels.pdf?code=C1%20S01").status_code, 200)

    def test_select_locations_matches_the_page_order(self):
        for params in ({"group": "C1"}, {"q": "S0"}):
            html = self.client.get("/locations", query_string=params).get_data(as_text=True)
            shown = re.findall(r"<summary>\s*<code>([^<]*)</code>", html)
            self.assertTrue(shown)
            self.assertEqual(self.codes("/locations", **params), [code.replace("&#39;", "'") for code in shown])

    def test_label_url(self):
        with main.app.test_request_context("/"):
            self.assertEqual(main.label_url("scan_location", code="C1 S01"), "http://localhost/l/C1%20S01")
            main.set_setting("labels.base_url", "https://tally.example.lan/parts")
            self.assertEqual(main.label_url("scan_location", code="C1 S01"), "https://tally.example.lan/parts/l/C1%20S01")

    def test_scan_redirects(self):
        target = self.client.get("/l/c1%20s01")
        self.assertEqual(target.location, f"/stock?location={self.location('C1 S01')}")
        missing = self.client.get("/l/NOPE%201", follow_redirects=False)
        self.assertEqual(missing.location, "/locations?q=NOPE+1")
        self.assertIn("NOPE 1", self.client.get(missing.location).get_data(as_text=True))
        lower, upper = self.add_location("a1"), self.add_location("A1")
        self.assertEqual(self.client.get("/l/a1").location, f"/stock?location={lower}")
        self.assertEqual(self.client.get("/l/A1").location, f"/stock?location={upper}")

    def test_stock_filters_by_location(self):
        a1, a10 = self.add_location("A1"), self.add_location("A10")
        self.add_item("Alpha part", a1)
        self.add_item("Beta part", a10)
        html = self.client.get(f"/stock?location={a1}").get_data(as_text=True)
        self.assertIn('data-name="Alpha part"', html)
        self.assertNotIn('data-name="Beta part"', html)
        self.assertEqual(self.client.get("/stock?location=999999").location, "/locations")
        self.assertIn(f"/items/new?location={self.location('C1 S01')}", self.client.get(f"/stock?location={self.location('C1 S01')}").get_data(as_text=True))

    def test_checkout_stays_on_the_drawer(self):
        a1 = self.add_location("A1")
        item = self.add_item("Alpha part", a1)
        cart = lambda quantity: '[{"id": %d, "quantity": %s}]' % (item, quantity)
        for quantity in ("2", "99"):
            response = self.client.post("/stock/checkout", data=dict(cart=cart(quantity), action="stock-out", location=a1))
            self.assertEqual(response.location, f"/stock?location={a1}")
        self.assertEqual(self.client.post("/stock/checkout", data=dict(cart=cart("1"), location=9999)).location, "/stock")

    def test_item_label(self):
        item = self.add_item("10 kΩ ±5% resistor", self.location("C1 S01"))
        response = self.client.get(f"/items/{item}/label.pdf")
        self.assertEqual((response.status_code, response.mimetype), (200, "application/pdf"))
        self.assertEqual(self.pages(response), 1)
        self.assertEqual(self.client.get("/items/99999/label.pdf").status_code, 404)

    def test_label_settings_are_validated(self):
        for bad in ("ftp://tally.lan", "javascript:alert(1)", "https://tally.lan/?x=1", "https://tally.lan/#a"):
            self.client.post("/settings/labels", data=dict(base_url=bad, preset="a4-3x7"))
            self.assertEqual(self.query("SELECT 1 FROM settings WHERE key='labels.base_url'"), [], bad)
        self.client.post("/settings/labels", data=dict(base_url="https://tally.example.lan/parts/", preset="roll-62x29"))
        self.assertEqual(self.query("SELECT value FROM settings WHERE key LIKE 'labels.%' ORDER BY key"), [("https://tally.example.lan/parts",), ("roll-62x29",)])

    def test_settings_warns_about_unreachable_address(self):
        self.client.post("/settings/labels", data=dict(base_url="http://127.0.0.1:8000", preset="a4-3x7"))
        self.assertIn("badge warn", self.client.get("/settings/labels").get_data(as_text=True))
        self.client.post("/settings/labels", data=dict(base_url="https://tally.example.lan", preset="a4-3x7"))
        self.assertNotIn("badge warn", self.client.get("/settings/labels").get_data(as_text=True))

    def test_exports_flag_gates_printing_not_scanning(self):
        item = self.add_item("Part", self.location("C1 S01"))
        self.assertIn("Print label", self.client.get(f"/items/{item}").get_data(as_text=True))
        self.assertIn("Print all labels", self.client.get("/locations").get_data(as_text=True))
        self.client.post("/settings/features", data=dict(images="1"))
        for path in ("/locations/labels.pdf?group=C1", f"/items/{item}/label.pdf"):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertNotIn("Print label", self.client.get(f"/items/{item}").get_data(as_text=True))
        self.assertNotIn("Print all labels", self.client.get("/locations").get_data(as_text=True))
        self.assertEqual(self.client.get("/l/C1%20S01").status_code, 302)


if __name__ == "__main__":
    unittest.main()
