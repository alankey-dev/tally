import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app.main as main


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        for name in ("Perfboard", "Breadboard", "Jumper wires", "White LED"):
            self.client.post("/items/new", data=dict(name=name, quantity="100", unit="pcs", location_id=location))

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def checkout(self, *ids, action="stock-out", quantity=1):
        cart = json.dumps([{"id": i, "quantity": quantity} for i in ids])
        return self.client.post("/stock/checkout", data=dict(cart=cart, action=action))

    def report(self, **params):
        with main.app.test_request_context():
            return main.usage_report(main.db(), params.get("days", "90"), params.get("group", ""))

    def test_returns_do_not_cancel_a_checkout(self):
        for _ in range(3):
            self.checkout(1)
            self.checkout(1, action="return")
        self.checkout(2, action="stock-in")
        most = self.report()["most"]
        self.assertEqual([(row["name"], row["checkouts"]) for row in most], [("Perfboard", 3)])

    def test_lost_and_builds_count_but_stocktake_and_adjustments_do_not(self):
        self.checkout(2, action="lost")
        self.client.post("/items/3", data=dict(quantity_change="-5", reason="Gave away"))
        with main.app.app_context():
            conn = main.db()
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=4").fetchone()
            with conn:
                main.record_movement(conn, item, -1, "Built Blinky ×1", "stock.out")
                main.record_movement(conn, item, -2, "Stocktake", "stock.out")
        names = sorted(row["name"] for row in self.report()["most"])
        self.assertEqual(names, ["Breadboard", "White LED"])

    def test_least_used_lists_untouched_stock_with_never_first(self):
        self.checkout(1)
        least = [row["name"] for row in self.report()["least"]]
        self.assertEqual(sorted(least), ["Breadboard", "Jumper wires", "White LED"])
        self.assertNotIn("Perfboard", least)

    def test_period_excludes_old_checkouts(self):
        self.checkout(1)
        with main.app.app_context():
            conn = main.db()
            conn.execute("UPDATE movements SET occurred_at='2020-01-01T00:00:00+00:00'")
            conn.commit()
        self.assertEqual(self.report()["most"], [])
        self.assertEqual([row["name"] for row in self.report(days="all")["most"]], ["Perfboard"])

    def test_one_checkout_action_shares_a_batch_and_pairs_need_three_jobs(self):
        for _ in range(2):
            self.checkout(1, 2, 3)
        self.assertEqual(len({row[0] for row in self.query("SELECT batch FROM movements WHERE reason='Stock used'")}), 2)
        self.assertEqual(self.report()["together"], [])
        self.checkout(1, 2)
        pairs = {(a["name"], b["name"]): jobs for a, b, jobs in self.report()["together"]}
        self.assertEqual(pairs, {("Perfboard", "Breadboard"): 3})

    def test_shared_project_counts_as_one_job_even_when_built(self):
        pages = [self.client.post("/projects", data=dict(title=f"P{n}")).headers["Location"] for n in range(3)]
        for page in pages:
            for item in (1, 4):
                self.client.post(page, data=dict(item_id=item, per_build="1"))
        self.client.post(pages[0] + "/build", data=dict(quantity="2"))
        pairs = {(a["name"], b["name"]): jobs for a, b, jobs in self.report()["together"]}
        self.assertEqual(pairs, {("Perfboard", "White LED"): 3})

    def test_stocktake_shortfall_is_listed_apart_from_the_ranking(self):
        with main.app.app_context():
            conn = main.db()
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=1").fetchone()
            with conn:
                main.record_movement(conn, item, -4, "Stocktake", "stock.out")
        report = self.report()
        self.assertEqual([(row["name"], row["missing"]) for row in report["short"]], [("Perfboard", 4)])
        self.assertEqual(report["most"], [])
        self.assertEqual(report["coverage"], 0)

    def test_coverage_is_the_share_of_removed_stock_that_was_recorded(self):
        self.checkout(1, quantity=6)
        with main.app.app_context():
            conn = main.db()
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=2").fetchone()
            with conn:
                main.record_movement(conn, item, -4, "Stocktake", "stock.out")
        self.assertEqual(self.report()["coverage"], 60)

    def test_group_filter_limits_every_list(self):
        self.checkout(1)
        self.assertEqual(len(self.report(group="4L")["most"]), 1)
        self.assertEqual(self.report(group="C1")["most"], [])

    def test_pages_render_and_csv_follows_the_exports_setting(self):
        self.checkout(1)
        html = self.client.get("/reports/usage").get_data(as_text=True)
        self.assertIn("Perfboard", html)
        self.assertIn("100% of stock taken out was recorded", html)
        self.assertEqual(self.client.get("/reports/usage?days=bogus&group=%27").status_code, 200)
        self.assertIn("Usage", self.client.get("/reports").get_data(as_text=True))
        self.assertIn("Perfboard", self.client.get("/reports/usage.csv").get_data(as_text=True))
        with main.app.app_context():
            main.db().execute("INSERT INTO settings(key, value) VALUES ('feature.exports', '0')")
            main.db().commit()
        self.assertEqual(self.client.get("/reports/usage.csv").status_code, 404)

    def test_batch_migration_is_safe_to_run_twice_and_old_rows_stay_null(self):
        conn = sqlite3.connect(main.DB_PATH)
        conn.execute("ALTER TABLE movements DROP COLUMN batch")
        conn.execute("INSERT INTO movements(item_id, quantity_change, reason, occurred_at) VALUES (1, -1, 'Stock used', '2026-01-01T00:00:00+00:00')")
        conn.commit()
        conn.close()
        self.client.get("/")
        self.client.get("/")
        self.assertEqual(self.query("SELECT batch FROM movements WHERE occurred_at LIKE '2026-01-01%'"), [(None,)])


if __name__ == "__main__":
    unittest.main()
