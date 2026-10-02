import re
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import app.main as main
from app.bag_labels import parse_bag_label

GS, RS, EOT = "\x1d", "\x1e", "\x04"
DIGIKEY = f"[)>{RS}06{GS}P1234-ND{GS}1PNE555P{GS}30P296-1411-5-ND{GS}K{GS}1K9876{GS}Q25{GS}11K1{RS}{EOT}"
MOUSER = f"[)>{RS}06{GS}K{GS}14K1{GS}1PNE555P{GS}Q10{GS}4LCN{RS}{EOT}"
LCSC = "{pbn:PM1,on:SO1,pc:C7593,pm:NE555P,qty:50,mc:,cc:1}"
TME = "QTY:100 PN:NE555P-TME PO:1/2 MPN:NE555P MFR:Texas Instruments"


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/quick-add")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        self.client.post("/items/new", data=dict(name="Timer", part_number=" ne555p ", quantity="2", unit="pcs", location_id=self.location))
        self.client.post("/items/new", data=dict(name="Blank", quantity="1", unit="pcs", location_id=self.location))

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql)]

    def receive(self, **extra):
        html = self.client.get("/quick-add?item=1&q=NE555P").get_data(as_text=True)
        fields = {name: re.search('name="' + name + '" value="([^"]+)"', html)[1] for name in ("token", "csrf")}
        return {**fields, "item": "1", "q": "NE555P", "quantity": "25", **extra}

    def test_parser_formats(self):
        self.assertEqual(parse_bag_label(DIGIKEY), dict(manufacturer="", part_number="NE555P", supplier="Digi-Key", supplier_sku="296-1411-5-ND", quantity=25))
        self.assertEqual(parse_bag_label(MOUSER), dict(manufacturer="", part_number="NE555P", supplier="", supplier_sku="", quantity=10))
        self.assertEqual(parse_bag_label(LCSC), dict(manufacturer="", part_number="NE555P", supplier="LCSC", supplier_sku="C7593", quantity=50))
        self.assertEqual(parse_bag_label(TME), dict(manufacturer="Texas Instruments", part_number="NE555P", supplier="TME", supplier_sku="NE555P-TME", quantity=100))
        self.assertEqual(parse_bag_label(DIGIKEY.replace(RS, "").replace(EOT, ""))["quantity"], 25)
        self.assertEqual(parse_bag_label(DIGIKEY.rstrip(RS + EOT) + "\n")["part_number"], "NE555P")
        self.assertEqual(parse_bag_label(f"[)>{RS}06{GS}1PX{GS}Q0")["quantity"], 1)
        for plain in ("ESP32", "5012345678900", "", "QTY: lots"):
            self.assertIsNone(parse_bag_label(plain))
        with self.assertRaises(ValueError):
            parse_bag_label(f"[)>{RS}061PNE555PQ25")

    def test_unique_match_redirects_to_the_receive_form(self):
        response = self.client.get("/quick-add", query_string={"q": DIGIKEY})
        self.assertEqual(response.status_code, 302)
        self.assertRegex(response.location, r"item=1&.*quantity=25")
        self.assertIn("q=NE555P", response.location)
        html = self.client.get(response.location).get_data(as_text=True)
        self.assertIn('name="quantity" value="25"', html)
        self.assertIn('name="supplier_sku" value="296-1411-5-ND"', html)
        self.assertNotRegex(re.search(r'<input[^>]*id="quick-query"[^>]*>', html)[0], "autofocus")
        self.assertIn("autofocus", re.search(r'<input[^>]*quantity-input[^>]*>', html)[0])

    def test_matching_rules_and_long_labels(self):
        self.assertIn("item=1", self.client.get("/quick-add", query_string={"q": LCSC.replace("NE555P", "Ne555P")}).location)
        blank = f"[)>{RS}06{GS}1P{GS}Q5{RS}{EOT}"
        self.assertNotIn("item=", self.client.get("/quick-add", query_string={"q": blank}).location)
        long_label = f"[)>{RS}06{GS}1PNE555P{GS}Q4{GS}1K{'9' * 300}{RS}{EOT}"
        self.assertGreater(len(long_label), 200)
        self.assertIn("item=1", self.client.get("/quick-add", query_string={"q": long_label}).location)
        self.assertEqual(self.client.get("/quick-add", query_string={"q": DIGIKEY, "fragment": "1"}).status_code, 200)
        flashed = self.client.get("/quick-add", query_string={"q": f"[)>{RS}061PNE555P"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Set your scanner to send GS", flashed)

    def test_sku_match_when_the_part_number_is_unknown(self):
        self.client.post("/quick-add", data=self.receive(supplier="Digi-Key", supplier_sku="296-1411-5-ND"))
        label = f"[)>{RS}06{GS}1PUNKNOWN{GS}30P296-1411-5-ND{GS}Q5"
        self.assertIn("item=1", self.client.get("/quick-add", query_string={"q": label}).location)

    def test_unknown_part_number_leads_to_a_prefilled_new_component(self):
        label = TME.replace("NE555P", "LM358")
        location = self.client.get("/quick-add", query_string={"q": label}).location
        self.assertNotIn("item=", location)
        html = self.client.get(location).get_data(as_text=True)
        link = re.search(r'href="(/items/new\?[^"]*)">Describe', html)[1].replace("&amp;", "&")
        self.assertIn("part_number=LM358", link)
        form = self.client.get(link).get_data(as_text=True)
        self.assertIn('name="part_number" id="item-part-number" value="LM358"', form)
        self.assertIn('name="quantity" type="number" step="any" min="0" inputmode="decimal" value="100"', form)
        self.client.post("/items/new", data=dict(name="Op amp", family="generic", manufacturer="Texas Instruments", part_number="LM358", quantity="100", unit="pcs", location_id=self.location, supplier="TME", supplier_sku="LM358-TME"))
        self.assertEqual(self.query("SELECT manufacturer, part_number, supplier, supplier_sku FROM items WHERE name='Op amp'"), [("Texas Instruments", "LM358", "TME", "LM358-TME")])

    def test_catalogue_entry_with_the_same_part_number(self):
        with main.app.app_context():
            entry = main.db().execute("SELECT id, part_number FROM catalogue WHERE part_number!='' LIMIT 1").fetchone()
        label = f"[)>{RS}06{GS}1P{entry['part_number'].lower()}{GS}Q3"
        self.assertIn(f"catalogue={entry['id']}", self.client.get("/quick-add", query_string={"q": label}).location)

    def test_receipt_saves_the_sku_once_and_returns_to_an_empty_search(self):
        fields = self.receive(scan="1", expected="25", supplier="Digi-Key", supplier_sku="296-1411-5-ND")
        for _ in range(2):
            response = self.client.post("/quick-add", data=fields)
            self.assertEqual(response.location, "/quick-add")
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stock received'"), [(1,)])
        self.assertEqual(self.query("SELECT supplier, supplier_sku, quantity FROM items WHERE id=1"), [("Digi-Key", "296-1411-5-ND", 27)])
        self.client.post("/quick-add", data={**self.receive(scan="1", expected="25", supplier="LCSC", supplier_sku="C7593")})
        self.assertEqual(self.query("SELECT supplier, supplier_sku FROM items WHERE id=1"), [("Digi-Key", "296-1411-5-ND")])

    def test_quantity_far_above_the_label_is_not_recorded(self):
        response = self.client.post("/quick-add", data=self.receive(scan="1", expected="25", quantity="251"))
        self.assertIn("Check the quantity", response.get_data(as_text=True))
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stock received'"), [(0,)])
        self.assertEqual(self.client.post("/quick-add", data=self.receive(scan="1", expected="25", quantity="250")).status_code, 302)

    def test_webhook_and_item_page(self):
        with mock.patch.object(main, "dispatch_webhooks") as dispatch:
            self.client.post("/quick-add", data=self.receive(scan="1", expected="25", supplier="Digi-Key", supplier_sku="296-1411-5-ND"))
        self.assertEqual([call.args[0] for call in dispatch.call_args_list], ["stock.in"])
        html = self.client.get("/items/1").get_data(as_text=True)
        self.assertIn("Supplier SKU", html)
        self.assertIn("296-1411-5-ND (Digi-Key)", html)
        self.assertNotIn("Supplier SKU", self.client.get("/items/2").get_data(as_text=True))

    def test_legacy_database_gains_the_columns(self):
        with main.app.app_context():
            conn = main.db()
            conn.execute("ALTER TABLE items DROP COLUMN supplier")
            conn.execute("ALTER TABLE items DROP COLUMN supplier_sku")
            conn.commit()
            main.init_db()
            main.init_db()
        columns = {row[1] for row in sqlite3.connect(main.DB_PATH).execute("PRAGMA table_info(items)")}
        self.assertLessEqual({"supplier", "supplier_sku"}, columns)
        self.assertEqual(self.query("SELECT quantity FROM items ORDER BY id"), [(2,), (1,)])
