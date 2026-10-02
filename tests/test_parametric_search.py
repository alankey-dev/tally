import re
import tempfile
import unittest
from pathlib import Path

import app.main as main
from app.matching import parse_value, value_token


class ValueTests(unittest.TestCase):
    def test_parse_value(self):
        for written in ("4k7", "4.7k", "4700R", "4700 Ω"):
            self.assertEqual(parse_value(written), 4700)
        for written in ("0.1u", "100n", "100nF", "100 nF", "0.1µF"):
            self.assertAlmostEqual(parse_value(written), 1e-7)
        self.assertEqual(parse_value("2R2"), 2.2)
        self.assertEqual(parse_value("10K"), 10000)
        self.assertEqual(parse_value("0R"), 0)
        for junk in ("", "abc", "4700", "1m", "1N4148", "10k 0603"):
            self.assertIsNone(parse_value(junk), junk)

    def test_value_token(self):
        self.assertEqual(value_token(parse_value("10u"), "F"), "10u")
        self.assertEqual(value_token(parse_value("1M")), "1meg")
        self.assertEqual(value_token(parse_value("1mF"), "F"), "1m")
        self.assertEqual(value_token(parse_value("4700R")), "4k7")
        self.assertEqual(value_token(parse_value("2R2")), "2r2")


class ParametricSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/quick-add")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        self.add("Resistor", "resistor", value="10", value_unit="kΩ", package="0603")
        self.add("Resistor", "resistor", value="100", value_unit="kΩ", package="0603")
        self.add("Resistor", "resistor", value="10", value_unit="kΩ", package="0805")
        self.add("Resistor", "resistor", value="4.7", value_unit="kΩ", package="0805")
        self.add("Resistor", "resistor", value="4.7", value_unit="kΩ", package="1206")
        self.add("Capacitor", "capacitor", value="100", value_unit="nF", voltage="50 V", package="0805")
        self.add("Capacitor", "capacitor", value="100", value_unit="nF", voltage="16 V", package="0603")
        self.add("Capacitor", "capacitor", value="47", value_unit="µF", voltage="50V", package="0805")
        self.add("Capacitor", "capacitor", value="0.01", value_unit="mF", voltage="6.3 V", package="1206")
        self.add("Capacitor", "capacitor", value="1", value_unit="mF", voltage="10 V", package="radial")
        self.add("100 nF capacitor", "generic")
        self.add("1N4148 diode", "semiconductor")
        self.add("2N2222 transistor", "semiconductor")
        self.add("ESP32-C3 board", "microcontroller")
        self.add("Uno R3", "microcontroller", part_number="A000066")
        self.add("3M Kapton tape", "generic")

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def add(self, name, family, part_number="", **attributes):
        data = dict(name=name, family=family, quantity="2", unit="pcs", location_id=self.location, part_number=part_number)
        data.update({f"attr_{key}": value for key, value in attributes.items()})
        self.client.post("/items/new", data=data)

    def search(self, query):
        return self.client.get("/api/search", query_string={"q": query}).json

    def names(self, query):
        return [(row["name"], row["summary"]) for row in self.search(query)]

    def page(self, url, **query):
        return self.client.get(url, query_string=query).get_data(as_text=True)

    def test_value_and_package(self):
        self.assertEqual(self.names("10k 0603"), [("Resistor", "10 kΩ · 0603")])
        self.assertEqual(self.names("4700R"), [("Resistor", "4.7 kΩ · 0805"), ("Resistor", "4.7 kΩ · 1206")])

    def test_values_written_either_way(self):
        found = {tuple(row["summary"] for row in self.search(query)) for query in ("0.1uF 0603", "100n 0603", "100 nF 0603")}
        self.assertEqual(found, {("100 nF · 16 V · 0603",)})
        self.assertNotIn("Resistor", [row["name"] for row in self.search("100n")])
        self.assertNotIn("1 mF · 10 V · radial", [row["summary"] for row in self.search("1M")])
        self.assertIn("100 nF capacitor", [row["name"] for row in self.search("0.1u")])

    def test_voltage_terms(self):
        for query in ("100nF 50V", "100 nF 50 V"):
            self.assertEqual(self.names(query), [("Capacitor", "100 nF · 50 V · 0805")])

    def test_regressions(self):
        for query, name in (("1N4148", "1N4148 diode"), ("1n41", "1N4148 diode"), ("ESP32-C3", "ESP32-C3 board"), ("Uno R3", "Uno R3"), ("3M", "3M Kapton tape")):
            self.assertIn(name, [row["name"] for row in self.search(query)], query)
        self.assertNotIn("2N2222 transistor", [row["name"] for row in self.search("2.2n")])

    def test_quick_add_shows_summary(self):
        html = self.page("/quick-add", q="4k7")
        self.assertIn("4.7 kΩ · 0805", html)
        self.assertIn("4.7 kΩ · 1206", html)

    def test_items_search_and_family(self):
        self.assertNotIn("10 kΩ · 0805", self.page("/items", q="10k 0603"))
        self.assertIn("10 kΩ · 0603", self.page("/items", q="10k 0603"))
        html = self.page("/items", family="capacitor", q="0805")
        self.assertNotIn("kΩ", html)
        self.assertIn("100 nF · 50 V · 0805", html)
        self.assertRegex(self.page("/items"), r"Capacitor <span class=\"badge neutral\">5</span>")

    def test_attribute_chips(self):
        html = self.page("/items", family="capacitor", attr_package="0805")
        self.assertIn("100 nF · 50 V · 0805", html)
        self.assertIn("47 µF · 50V · 0805", html)
        self.assertNotIn("0603", html.split('class="component-grid"')[1])
        self.assertIn('aria-label="Package / size"', html)
        self.assertEqual(len(re.findall(r">(?:50 V|50V) <span", html)), 1)
        self.assertNotIn(">0603 <span", self.page("/items", family="resistor", attr_package="0805").split("Package / size")[1].split("</div>")[0])

    def test_range(self):
        html = self.page("/items", family="capacitor", min="10u")
        self.assertIn("47 µF", html)
        self.assertNotIn("100 nF · 16 V", html)
        self.assertIn("0.01 mF", self.page("/items", family="capacitor", max="10u"))
        response = self.client.get("/items?family=capacitor&min=abc")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Choose the Resistor or Capacitor family", response.get_data(as_text=True))
        html = self.page("/items", min="10u")
        self.assertIn("Choose the Resistor or Capacitor family", html)
        self.assertIn("4.7 kΩ · 1206", html)

    def test_filters_are_kept(self):
        html = self.page("/items", family="resistor", attr_package="0805")
        self.assertRegex(html, r'<input type="hidden" name="attr_package" value="0805">')
        self.assertIn("view=list", re.search(r'href="([^"]*view=list[^"]*)"', html)[1])
        self.assertIn("attr_package=0805", re.search(r'href="([^"]*view=list[^"]*)"', html)[1])
        range_form = html.split('class="filter-range"')[1].split("</form>")[0]
        self.assertIn('name="attr_package" value="0805"', range_form)

    def test_bad_attributes_do_not_break_search(self):
        with main.app.app_context():
            main.db().execute("UPDATE items SET attributes='{' WHERE family='resistor'")
            main.db().commit()
        self.assertEqual(self.client.get("/items?family=resistor&attr_package=0805").status_code, 200)
        self.assertEqual(self.client.get("/api/search?q=10k").status_code, 200)

    def test_tile_without_part_number_shows_summary(self):
        tile = self.page("/items", family="resistor").split('class="component-card-body"')[1]
        self.assertIn("<p>10 kΩ · 0603</p>", tile)


if __name__ == "__main__":
    unittest.main()
