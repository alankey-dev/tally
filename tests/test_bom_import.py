import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.main as main
from app.bom import expand_designators, value_text

HEADER = "Reference,Value,Footprint,Qty,MPN,LCSC Part #\n"


class BomImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        for name, part_number in (("10k resistor 0805", "C25804"), ("10 kΩ resistor 0805", ""), ("10k resistor 1206", ""), ("100nF capacitor", ""), ("4K7 resistor", ""), ("Blank part", "")):
            self.client.post("/items/new", data=dict(name=name, part_number=part_number, quantity="10", unit="pcs", location_id=self.location))
        self.project = self.client.post("/projects", data=dict(title="Blinky")).headers["Location"]
        self.id = int(self.project.rsplit("/", 1)[1])

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def upload(self, raw, name="bom.csv"):
        return self.client.post(f"/projects/{self.id}/import", data={"file": (io.BytesIO(raw), name)}, content_type="multipart/form-data", follow_redirects=True)

    def lines(self, html):
        return json.loads(re.search(r"name=\"lines\" value='([^']*)'", html)[1].replace("\\u0027", "'"))

    def commit(self, lines, choices, per_build=None, **extra):
        data = {"lines": json.dumps(lines), **extra}
        for n, choice in enumerate(choices):
            data[f"item_{n}"] = str(choice)
            data[f"per_build_{n}"] = str((per_build or {}).get(n, lines[n]["quantity"]))
        return self.client.post(f"/projects/{self.id}/import/commit", data=data, follow_redirects=True)

    def test_kicad_csv_is_grouped_and_matched_by_part_number(self):
        html = self.upload((HEADER + '"R1, R2, R3",10k,R_0805_2012Metric,3,,C25804\n').encode()).get_data(as_text=True)
        self.assertEqual(len(self.lines(html)), 1)
        self.assertEqual(self.lines(html)[0]["quantity"], 3)
        self.assertIn("Part number", html)
        self.assertRegex(html, r'<option value="1" selected>')
        self.assertIn('name="per_build_0" type="number" min="0.001" step="any" value="3"', html)
        self.assertEqual(self.query("SELECT * FROM project_items"), [])

    def test_utf16_tab_and_cp1252_files_are_read(self):
        table = "Designator\tComment\tFootprint\tLCSC Part #\nR1, R2\t10k\tR_0805\tC25804\nC1\t100 µF\tC_0805\t\n"
        first = self.lines(self.upload(table.encode("utf-16")).get_data(as_text=True))
        second = self.lines(self.upload(table.replace("\t", ";").encode("cp1252")).get_data(as_text=True))
        self.assertEqual(first, second)
        self.assertEqual([line["quantity"] for line in first], [2, 1])
        self.assertEqual(first[1]["value"], "100 µF")

    def test_semicolon_file_with_quoted_designators(self):
        html = self.upload(b'Reference;Value;Qty\n"R1, R2";10k;2\n').get_data(as_text=True)
        self.assertEqual(self.lines(html)[0]["designators"], "R1, R2")

    def test_lcsc_code_matches_part_number(self):
        html = self.upload(b"Designator,Comment,LCSC Part #\nR9,whatever,c25804\n").get_data(as_text=True)
        self.assertIn("Part number", html)
        self.assertRegex(html, r'<option value="1" selected>')

    def test_values_match_items_written_either_way(self):
        html = self.upload(b"Reference,Value,Footprint\nR1,10k,R_0805_2012Metric\nC1,100n,C_0805\nR2,4k7,\n").get_data(as_text=True)
        selects = re.findall(r'<select name="item_\d+".*?</select>', html, re.S)
        options = [re.findall(r"<option value=\"\d+\"[^>]*>([^<]*?) ·", select) for select in selects]
        self.assertEqual(sorted(options[0][:2]), ["10 kΩ resistor 0805", "10k resistor 0805"])
        self.assertEqual(options[0][2], "10k resistor 1206")
        self.assertEqual(options[1][0], "100nF capacitor")
        self.assertEqual(options[2][0], "4K7 resistor")

    def test_blank_mpn_does_not_match_items_without_part_number(self):
        for mpn in ("~", ""):
            html = self.upload(f"Reference,Value,MPN\nU1,zzzz,{mpn}\n".encode()).get_data(as_text=True)
            self.assertNotIn("Part number</span>", html)
            self.assertIn("No match", html)
            self.assertIn('value="skip" selected', html)

    def test_commit_adds_lines_without_moving_stock(self):
        lines = self.lines(self.upload((HEADER + "R1 R2,10k,,2,,C25804\nC1,100n,,1,,\n").encode()).get_data(as_text=True))
        html = self.commit(lines, [1, 4], {1: "1.5"}).get_data(as_text=True)
        self.assertIn("Added 2 lines, skipped 0.", html)
        self.assertIn("Stock is only taken when you build", html)
        self.assertEqual(self.query("SELECT item_id, per_build, quantity FROM project_items ORDER BY item_id"), [(1, 2, 0), (4, 1.5, 0)])
        self.assertEqual(self.query("SELECT quantity FROM items ORDER BY id"), [(10,)] * 6)
        self.assertEqual(self.query("SELECT COUNT(*) FROM movements"), [(6,)])
        self.assertIn("<strong>5</strong>", html)

    def test_skipped_duplicate_and_existing_lines(self):
        self.client.post(self.project, data=dict(item_id=1, per_build="9"))
        lines = self.lines(self.upload(b"Reference,Value,Qty\nR1,10k,2\nR2,10 k,3\nR3,1M,4\n").get_data(as_text=True))
        page = self.upload(b"Reference,Value,MPN\nR1,10k,C25804\n").get_data(as_text=True)
        self.assertIn("Already a line, ×9", page)
        html = self.commit(lines, [1, 1, "skip"]).get_data(as_text=True)
        self.assertIn("Added 2 lines, skipped 1. 1 shared an item", html)
        self.assertEqual(self.query("SELECT item_id, per_build FROM project_items"), [(1, 5)])
        self.assertIn("Nothing added", self.commit(lines, ["skip"] * 3).get_data(as_text=True))

    def test_bad_line_writes_nothing(self):
        lines = self.lines(self.upload(b"Reference,Value,Qty\nR1,10k,2\nC1,100n,1\n").get_data(as_text=True))
        for item, quantity in ((999, "1"), ("abc", "1"), (2, "0"), (2, "-1"), (2, "nan"), (2, "x")):
            html = self.commit(lines, [1, item], {1: quantity}).get_data(as_text=True)
            self.assertIn('badge danger">Choose an item', html)
            self.assertEqual(self.query("SELECT * FROM project_items"), [])

    def test_bad_files_write_nothing(self):
        big = b"Reference,Value\n" + b"R1,10k\n" * (main.MAX_BOM_BYTES // 7 + 1)
        many = "Reference,Value\n" + "".join(f"R{n},{n}k\n" for n in range(main.MAX_BOM_LINES + 1))
        for raw in (b"\x00\x01\x02binary", b"a,b\n1,2\n", big, many.encode(), b"Reference,Value\n", b""):
            self.assertIn(b'class="flash error', self.upload(raw).data)
        self.assertEqual(self.query("SELECT * FROM project_items"), [])

    def test_tampered_lines_field_is_rejected(self):
        good = {"designators": "R1", "value": "10k", "footprint": "", "mpn": "", "supplier_code": "", "quantity": 1, "suggested": None}
        for lines in ("not json", "{}", "[1]", json.dumps([good] * (main.MAX_BOM_LINES + 1)), json.dumps([{**good, "suggested": "x"}]),
                      json.dumps([{**good, "quantity": "1"}]), json.dumps([{**good, "value": "x" * 201}]), ""):
            for path in ("import", "import/commit"):
                response = self.client.post(f"/projects/{self.id}/{path}", data={"lines": lines, "item_0": "1", "per_build_0": "1"}, follow_redirects=True)
                self.assertIn(b'class="flash error', response.data)
        self.assertEqual(self.query("SELECT * FROM project_items"), [])
        self.assertEqual(self.client.post("/projects/99/import").status_code, 404)
        self.assertEqual(self.client.post("/projects/99/import/commit").status_code, 404)

    def test_match_again_keeps_hand_choices(self):
        lines = self.lines(self.upload(b"Reference,Value,Qty\nR1,10k,2\nC1,100n,1\nR2,4k7,1\n").get_data(as_text=True))
        data = {"lines": json.dumps(lines), "item_0": "3", "item_1": "1", "item_2": "5", "q_0": "", "q_1": "capacitor", "q_2": "", "per_build_0": "7", "per_build_1": "1", "per_build_2": "1"}
        # Line 0 was changed by hand, line 1 searched, line 2 left alone but with a stale choice of its own suggestion.
        data["item_2"] = str(lines[2]["suggested"] or "skip")
        html = self.client.post(f"/projects/{self.id}/import", data=data).get_data(as_text=True)
        self.assertRegex(html, r'<option value="3" selected>')
        self.assertIn('name="per_build_0" type="number" min="0.001" step="any" value="7"', html)
        self.assertIn('value="capacitor"', html)
        self.assertRegex(html, r'<option value="1" selected>')
        self.assertRegex(html, r'<option value="5" selected>')

    def test_new_item_link_follows_add_components(self):
        raw = b"Reference,Value,Footprint\nU1,zzzz,R_0805_2012Metric\n"
        self.assertIn('href="/items/new?name=zzzz+0805" target="_blank"', self.upload(raw).get_data(as_text=True))
        self.client.post("/settings/features", data={})
        self.assertNotIn("New item", self.upload(raw).get_data(as_text=True))

    def test_commit_of_max_lines_succeeds(self):
        lines = [{"designators": f"R{n}", "value": "10k", "footprint": "", "mpn": "", "supplier_code": "", "quantity": 1, "suggested": None} for n in range(main.MAX_BOM_LINES)]
        html = self.commit(lines, [1] * len(lines)).get_data(as_text=True)
        self.assertIn(f"Added {main.MAX_BOM_LINES} lines", html)
        self.assertEqual(self.query("SELECT per_build FROM project_items"), [(float(main.MAX_BOM_LINES),)])

    def test_items_are_normalised_once_per_request(self):
        import app.bom as bom
        with mock.patch.object(bom, "normalise", wraps=bom.normalise) as normalise:
            self.upload(b"Reference,Value\nR1,10k\nC1,100n\nR2,4k7\n")
        self.assertEqual(normalise.call_count, 6 + 3)

    def test_dnp_rows_are_skipped(self):
        html = self.upload(b"Reference,Value,DNP\nR1,10k,\nR2,10k,DNP\nR3,4k7,no\n").get_data(as_text=True)
        self.assertEqual([line["designators"] for line in self.lines(html)], ["R1", "R3"])
        self.assertIn("1 DNP row skipped", html)


class BomParserTests(unittest.TestCase):
    def test_designators_expand(self):
        self.assertEqual(expand_designators("R1-R4, R7"), ["R1", "R2", "R3", "R4", "R7"])
        self.assertEqual(expand_designators("C1 C2,C3"), ["C1", "C2", "C3"])
        self.assertEqual(expand_designators("R1 - R2"), ["R1", "R2"])
        self.assertEqual(expand_designators(""), [])

    def test_value_text_writes_values_one_way(self):
        for written in ("10k", "10K", "10kΩ", "10 kΩ", "10k ohm"):
            self.assertEqual(value_text(written).split(), ["10", "k"], written)
        for written in ("4k7", "4K7"):
            self.assertEqual(value_text(written).split(), ["4.7", "k"])
        for written in ("4R7", "4.7Ω"):
            self.assertEqual(value_text(written).split(), ["4.7"])
        for written in ("100n", "100nF", "100 nF"):
            self.assertEqual(value_text(written).split(), ["100", "n"])
        for written in ("10u", "10uF", "10µF", "10 μF"):
            self.assertEqual(value_text(written).split(), ["10", "u"])
