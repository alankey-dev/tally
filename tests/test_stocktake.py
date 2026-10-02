import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

import app.main as main


class StocktakeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        for name, quantity, unit in (("Blinky board", "10", "pcs"), ("100 nF capacitor", "7", "pcs"), ("Wire", "12.5", "m")):
            self.client.post("/items/new", data=dict(name=name, quantity=quantity, unit=unit, location_id=self.location))
        self.url = f"/locations/{self.location}/count"

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def stock(self):
        return [row[0] for row in self.query("SELECT quantity FROM items ORDER BY id")]

    def version(self):
        return self.client.get("/api/version").get_json()["version"]

    def count(self, url=None, **fields):
        data = {}
        for item_id in (1, 2, 3):
            data[f"expected-{item_id}"] = str(self.stock()[item_id - 1])
        data.update({key.replace("_", "-"): value for key, value in fields.items()})
        return self.client.post(url or self.url, data=data, follow_redirects=True)

    def test_count_page_lists_the_storage_location(self):
        html = self.client.get(self.url).get_data(as_text=True)
        for text in ("Blinky board", "100 nF capacitor", 'name="expected-1" value="10.0"', 'step="any"', "Matches"):
            self.assertIn(text, html)
        self.assertNotIn("data-live", html)
        self.assertIn('inputmode="numeric"', html)
        self.assertEqual(self.client.get("/locations/9999/count").status_code, 404)
        with main.app.app_context():
            main.db().execute("UPDATE items SET quantity=2.5 WHERE id=1")
            main.db().commit()
        self.assertIn('inputmode="decimal" name="count-1"', self.client.get(self.url).get_data(as_text=True))

    def test_difference_writes_one_stocktake_movement(self):
        with mock.patch.object(main, "dispatch_webhooks") as webhooks:
            html = self.count(count_1="8").get_data(as_text=True)
        self.assertIn("Counted 1 item in 4L 01: 1 difference recorded.", html)
        self.assertEqual(self.stock()[0], 8)
        self.assertEqual(self.query("SELECT quantity_change, reason FROM movements WHERE reason='Stocktake'"), [(-2, "Stocktake")])
        self.assertEqual([call.args[0] for call in webhooks.call_args_list], ["stock.out"])

    def test_match_stamps_without_moving_stock(self):
        before = self.query("SELECT updated_at FROM items WHERE id=1")
        marker = self.version()
        self.count(match_1="1")
        self.assertEqual(self.stock(), [10, 7, 12.5])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(0,)])
        self.assertIsNotNone(self.query("SELECT last_counted_at FROM items WHERE id=1")[0][0])
        self.assertEqual(self.query("SELECT updated_at FROM items WHERE id=1"), before)
        self.assertNotEqual(self.version(), marker)

    def test_blank_rows_are_left_alone(self):
        self.count(match_1="1")
        self.assertEqual(self.query("SELECT last_counted_at IS NULL FROM items ORDER BY id"), [(0,), (1,), (1,)])
        self.assertEqual(self.query("SELECT last_counted_at FROM locations WHERE id=?", self.location), [(None,)])

    def test_full_count_stamps_the_location(self):
        self.assertIn("4L 01", self.client.get("/").get_data(as_text=True).split("Due a count")[1].split("Recently updated")[0])
        self.count(match_1="1", count_2="7", count_3="12,5")
        self.assertIsNotNone(self.query("SELECT last_counted_at FROM locations WHERE id=?", self.location)[0][0])
        self.assertNotIn("4L 01", self.client.get("/").get_data(as_text=True).split("Due a count")[1].split("Recently updated")[0])
        self.client.post("/items/new", data=dict(name="New part", quantity="1", unit="pcs", location_id=self.location))
        self.assertIn("4L 01", self.client.get("/").get_data(as_text=True).split("Due a count")[1].split("Recently updated")[0])

    def test_old_counts_are_due_again(self):
        self.count(match_1="1", match_2="1", match_3="1")
        old = (datetime.now(timezone.utc) - timedelta(days=main.COUNT_DUE_DAYS + 1)).isoformat()
        with main.app.app_context():
            main.db().execute("UPDATE items SET last_counted_at=?", (old,))
            main.db().commit()
        self.assertIn("4L 01", self.client.get("/").get_data(as_text=True).split("Due a count")[1].split("Recently updated")[0])

    def test_invalid_counts_change_nothing(self):
        for bad in ("-1", "nan", "inf", "abc"):
            html = self.client.post(self.url, data={"expected-1": "10.0", "count-1": bad, "expected-2": "7.0", "count-2": "5"}).get_data(as_text=True)
            self.assertIn('aria-invalid="true"', html, bad)
            self.assertIn('value="5"', html)
            self.assertEqual(self.stock(), [10, 7, 12.5])
        response = self.client.post(self.url, data={"expected-1": "10.0", "count-1": "9", "match-1": "1", "expected-2": "7.0", "count-2": "5"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("badge danger", response.get_data(as_text=True))
        self.assertIn('value="5"', response.get_data(as_text=True))
        self.assertEqual(self.stock(), [10, 7, 12.5])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(0,)])

    def test_changed_stock_is_skipped(self):
        self.client.post("/items/1", data=dict(quantity_change="-1"))
        html = self.client.post(self.url, data={"expected-1": "10.0", "count-1": "8"}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Blinky board changed since you opened this page, count it again.", html)
        self.assertEqual(self.stock()[0], 9)
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(0,)])
        self.assertIsNone(self.query("SELECT last_counted_at FROM items WHERE id=1")[0][0])

    def test_replay_records_once(self):
        self.count(count_1="8")
        html = self.count(count_1="8").get_data(as_text=True)
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(1,)])
        self.assertNotIn("changed", html)

    def test_count_waits_for_a_concurrent_change_and_skips_it(self):
        other = sqlite3.connect(main.DB_PATH)
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE items SET quantity=quantity-2 WHERE id=1")
        results = []
        thread = threading.Thread(target=lambda: results.append(self.client.post(self.url, data={"expected-1": "10.0", "count-1": "6"}, follow_redirects=True).get_data(as_text=True)))
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive())
        other.commit()
        other.close()
        thread.join(10)
        self.assertIn("Blinky board changed since you opened this page", results[0])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(0,)])

    def test_fractional_units(self):
        self.count(count_3="12,25")
        self.assertEqual(self.query("SELECT quantity_change FROM movements WHERE reason='Stocktake'"), [(-0.25,)])
        with main.app.app_context():
            main.db().execute("UPDATE items SET quantity=0.1+0.2 WHERE id=3")
            main.db().commit()
        before = self.query("SELECT count(*) FROM movements")
        self.client.post(self.url, data={"expected-3": repr(0.1 + 0.2), "count-3": "0.3"})
        self.assertEqual(self.query("SELECT count(*) FROM movements"), before)
        self.assertIsNotNone(self.query("SELECT last_counted_at FROM items WHERE id=3")[0][0])

    def test_zero_is_a_real_count(self):
        self.count(count_2="0")
        self.assertEqual(self.stock()[1], 0)
        self.assertEqual(self.query("SELECT quantity_change FROM movements WHERE reason='Stocktake'"), [(-7,)])

    def test_walk_moves_to_the_next_location(self):
        with main.app.app_context():
            other = main.db().execute("SELECT id FROM locations WHERE code='4L 03'").fetchone()[0]
        self.client.post("/items/new", data=dict(name="Spare", quantity="1", unit="pcs", location_id=other))
        with main.app.app_context():
            sequence = [row["id"] for row in main.select_locations("4L") if row["item_count"]]
        self.assertEqual(sequence, [self.location, other])
        response = self.client.post(self.url + "?walk=1", data={})
        self.assertIn(f"/locations/{sequence[1]}/count?walk=1", response.headers["Location"])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Stocktake'"), [(0,)])
        last = self.client.post(f"/locations/{sequence[-1]}/count?walk=1", data={})
        self.assertIn("group=4L", last.headers["Location"])
        html = self.client.get(self.url + "?walk=1").get_data(as_text=True)
        self.assertIn("Save and next location", html)
        self.assertIn("Stop counting", html)
        self.assertIn("Count this group", self.client.get("/locations?group=4L").get_data(as_text=True))

    def test_confirm_empty_stamps_the_location(self):
        with main.app.app_context():
            empty = main.db().execute("SELECT id FROM locations WHERE code='4L 02'").fetchone()[0]
        self.assertIn("Nothing should be here", self.client.get(f"/locations/{empty}/count").get_data(as_text=True))
        marker = self.version()
        self.client.post(f"/locations/{empty}/count", data={"confirm_empty": "1"})
        self.assertIsNotNone(self.query("SELECT last_counted_at FROM locations WHERE id=?", empty)[0][0])
        self.assertNotEqual(self.version(), marker)
        html = self.client.post(self.url, data={"confirm_empty": "1"}, follow_redirects=True).get_data(as_text=True)
        self.assertIsNone(self.query("SELECT last_counted_at FROM locations WHERE id=?", self.location)[0][0])
        self.assertIn("Blinky board", html)

    def test_item_page_shows_last_counted(self):
        self.assertIn("Never counted", self.client.get("/items/1").get_data(as_text=True))
        self.count(match_1="1")
        self.assertIn(f"Last counted {datetime.now(timezone.utc).date().isoformat()}", self.client.get("/items/1").get_data(as_text=True))

    def test_migration_adds_columns(self):
        with main.app.app_context():
            conn = main.db()
            conn.executescript("""
                DROP TABLE attachments;
                ALTER TABLE items RENAME TO items_old;
                CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, manufacturer TEXT DEFAULT '', part_number TEXT DEFAULT '', quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'pcs', minimum_quantity REAL, location_id INTEGER NOT NULL, notes TEXT DEFAULT '', image_path TEXT DEFAULT '', updated_at TEXT NOT NULL, family TEXT NOT NULL DEFAULT 'generic', attributes TEXT NOT NULL DEFAULT '{}', supplier TEXT NOT NULL DEFAULT '', supplier_sku TEXT NOT NULL DEFAULT '', catalogue_id INTEGER);
                INSERT INTO items SELECT id, name, manufacturer, part_number, quantity, unit, minimum_quantity, location_id, notes, image_path, updated_at, family, attributes, supplier, supplier_sku, catalogue_id FROM items_old;
                DROP TABLE items_old;
                ALTER TABLE locations RENAME TO locations_old;
                CREATE TABLE locations (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, notes TEXT DEFAULT '', image_path TEXT DEFAULT '', created_at TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '');
                INSERT INTO locations SELECT id, code, kind, label, notes, image_path, created_at, keywords FROM locations_old;
                DROP TABLE locations_old;
            """)
            main.init_db()
            main.init_db()
            for table in ("items", "locations"):
                self.assertIn("last_counted_at", {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")})
        self.assertEqual(self.query("SELECT DISTINCT last_counted_at FROM items"), [(None,)])
        self.assertEqual(self.query("SELECT DISTINCT last_counted_at FROM locations"), [(None,)])


if __name__ == "__main__":
    unittest.main()
