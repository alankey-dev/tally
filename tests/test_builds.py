import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import app.main as main


class BuildPlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        for name, quantity in (("Blinky board", "10"), ("100 nF capacitor", "7")):
            self.client.post("/items/new", data=dict(name=name, quantity=quantity, unit="pcs", location_id=location))
        self.page = self.client.post("/projects", data=dict(title="Blinky")).headers["Location"]

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def stock(self):
        return [row[0] for row in self.query("SELECT quantity FROM items ORDER BY id")]

    def add_line(self, item_id, per_build):
        return self.client.post(self.page, data=dict(item_id=item_id, per_build=per_build), follow_redirects=True).get_data(as_text=True)

    def test_lines_are_a_recipe_and_report_builds_possible(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        self.assertEqual(self.stock(), [10, 7])
        html = self.client.get(self.page).get_data(as_text=True)
        self.assertIn("<strong>2</strong>", html)
        self.assertIn("builds possible from stock", html)
        self.assertIn("Covered", html)
        self.assertNotIn("Short", html)
        html = self.client.get(self.page, query_string={"quantity": "3"}).get_data(as_text=True)
        self.assertIn("Short 2", html)
        self.assertIn("×3", html)
        self.assertIn("2 builds possible", self.client.get("/projects").get_data(as_text=True))
        self.assertIn("2 builds", self.client.get("/").get_data(as_text=True))
        # Adding a component that is already a line replaces its per-build quantity.
        self.add_line(1, "6")
        self.assertEqual(self.query("SELECT per_build, quantity FROM project_items WHERE item_id=1"), [(6, 0)])
        self.assertIn("1 build possible", self.client.get("/projects").get_data(as_text=True))
        for invalid in ("0", "-1", "nan", "abc"):
            self.assertIn("how many one build needs", self.add_line(2, invalid))
        self.assertIn("how many one build needs", self.add_line(999, "1"))
        self.assertEqual(self.query("SELECT per_build FROM project_items WHERE item_id=2"), [(3,)])

    def test_build_takes_every_line_in_one_go(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        with mock.patch.object(main, "dispatch_webhooks") as webhooks:
            response = self.client.post(self.page + "/build", data=dict(quantity="2"), follow_redirects=True)
        self.assertIn("Built Blinky ×2: 2 lines taken from stock", response.get_data(as_text=True))
        self.assertEqual(self.stock(), [6, 1])
        self.assertEqual(self.query("SELECT item_id, quantity FROM project_items ORDER BY item_id"), [(1, 4), (2, 6)])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Built Blinky ×2' AND quantity_change<0"), [(2,)])
        quantity, lines, undone_at = self.query("SELECT quantity, lines, undone_at FROM builds")[0]
        self.assertEqual((quantity, json.loads(lines), undone_at), (2, [[2, 6], [1, 4]], None))
        self.assertEqual([call.args[0] for call in webhooks.call_args_list], ["stock.out", "stock.out", "project.built"])
        self.assertEqual(webhooks.call_args.args[1]["quantity"], 2)
        self.assertEqual([line["quantity_change"] for line in webhooks.call_args.args[1]["lines"]], [-6, -4])
        html = self.client.get(self.page).get_data(as_text=True)
        self.assertIn("Built ×2", html)
        self.assertIn("Undo", html)
        self.assertIn("<strong>0</strong>", html)
        for invalid in ("0", "-1", "abc", "1.5", ""):
            response = self.client.post(self.page + "/build", data=dict(quantity=invalid), follow_redirects=True)
            self.assertIn("whole number from 1", response.get_data(as_text=True))
        self.assertEqual(self.stock(), [6, 1])

    def test_short_build_moves_nothing(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        response = self.client.post(self.page + "/build", data=dict(quantity="3"), follow_redirects=True)
        self.assertIn("Not enough stock to build 3: 100 nF capacitor needs 9 pcs, 7 on hand.", response.get_data(as_text=True))
        self.assertEqual(self.stock(), [10, 7])
        self.assertEqual(self.query("SELECT count(*) FROM builds"), [(0,)])
        empty = self.client.post("/projects", data=dict(title="Empty")).headers["Location"]
        response = self.client.post(empty + "/build", data=dict(quantity="1"), follow_redirects=True)
        self.assertIn("Add at least one line", response.get_data(as_text=True))
        self.assertEqual(self.client.post("/projects/999/build", data=dict(quantity="1")).status_code, 404)

    def test_fractional_lines_are_covered_when_exactly_in_stock(self):
        self.client.post("/items/new", data=dict(name="Hook-up wire", quantity="0.3", unit="m", location_id=1))
        self.add_line(3, "0.1")
        html = self.client.get(self.page, query_string={"quantity": "3"}).get_data(as_text=True)
        self.assertIn("Covered", html)
        self.assertIn("<strong>3</strong>", html)
        self.client.post(self.page + "/build", data=dict(quantity="3"))
        self.assertAlmostEqual(self.stock()[2], 0)

    def test_undo_returns_what_the_build_took(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        self.client.post(self.page + "/build", data=dict(quantity="2"))
        # Changing the recipe afterwards must not change what comes back.
        self.client.post(self.page + "/lines/1", data=dict(per_build="9"))
        self.client.post(self.page + "/lines/2/delete")
        response = self.client.post(self.page + "/builds/1/undo", follow_redirects=True)
        self.assertIn("Build undone: 2 lines returned to stock", response.get_data(as_text=True))
        self.assertEqual(self.stock(), [10, 7])
        self.assertEqual(self.query("SELECT quantity FROM project_items WHERE item_id=1"), [(0,)])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Unbuilt Blinky ×2' AND quantity_change>0"), [(2,)])
        self.assertIsNotNone(self.query("SELECT undone_at FROM builds WHERE id=1")[0][0])
        self.assertIn("already undone", self.client.post(self.page + "/builds/1/undo", follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.stock(), [10, 7])
        self.assertEqual(self.client.post("/projects/999/builds/1/undo").status_code, 404)
        html = self.client.get(self.page).get_data(as_text=True)
        self.assertIn("Undone", html)
        self.assertNotIn(">Undo<", html)

    def test_lines_can_be_changed_and_removed_without_moving_stock(self):
        self.add_line(1, "2")
        self.client.post(self.page + "/lines/1", data=dict(per_build="5"))
        self.assertEqual(self.query("SELECT per_build FROM project_items"), [(5,)])
        for invalid in ("0", "-1", "inf", "abc"):
            response = self.client.post(self.page + "/lines/1", data=dict(per_build=invalid), follow_redirects=True)
            self.assertIn("greater than zero", response.get_data(as_text=True))
        self.assertEqual(self.query("SELECT per_build FROM project_items"), [(5,)])
        self.assertEqual(self.client.post(self.page + "/lines/2", data=dict(per_build="1")).status_code, 404)
        self.assertEqual(self.client.post(self.page + "/lines/1/delete").status_code, 302)
        self.assertEqual(self.query("SELECT count(*) FROM project_items"), [(0,)])
        self.assertEqual(self.client.post(self.page + "/lines/1/delete").status_code, 404)
        self.assertEqual(self.stock(), [10, 7])

    def test_bom_export_shows_recipe_and_consumption(self):
        self.add_line(1, "2")
        self.client.post(self.page + "/build", data=dict(quantity="1"))
        lines = self.client.get(self.page + "/bom.csv").get_data(as_text=True).splitlines()
        self.assertEqual(lines[0], "Component,Part number,Per build,Consumed,On hand,Unit,Location")
        self.assertEqual(lines[1], "Blinky board,,2.0,2.0,8.0,pcs,4L 01")
        self.assertTrue(self.client.get(self.page + "/bom.pdf").data.startswith(b"%PDF"))

    def test_lines_allocated_before_build_planning_become_one_consumed_build(self):
        with main.app.app_context():
            conn = main.db()
            conn.executescript("""
                DROP TABLE project_items;
                CREATE TABLE project_items (project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id), quantity REAL NOT NULL, PRIMARY KEY(project_id, item_id));
                INSERT INTO project_items VALUES (1, 1, 4), (1, 2, 1);
            """)
            main.init_db()
            main.init_db()
        self.assertEqual(self.query("SELECT item_id, quantity, per_build FROM project_items ORDER BY item_id"), [(1, 4, 4), (2, 1, 1)])
        quantity, lines, undone_at = self.query("SELECT quantity, lines, undone_at FROM builds")[0]
        self.assertEqual((quantity, json.loads(lines), undone_at), (1, [[1, 4], [2, 1]], None))
        self.assertEqual(self.stock(), [10, 7])
        html = self.client.get(self.page).get_data(as_text=True)
        self.assertIn("Built ×1", html)
        self.assertIn("<strong>2</strong>", html)
        self.client.post(self.page + "/builds/1/undo")
        self.assertEqual(self.stock(), [14, 8])

    def test_project_webhook_event_is_the_build(self):
        self.client.post("/settings/webhooks", data=dict(event="project.built", destination_url="https://example.test/hook"))
        self.assertEqual(self.query("SELECT event FROM webhooks"), [("project.built",)])
        self.client.post("/settings/webhooks", data=dict(event="project.component_added", destination_url="https://example.test/hook"))
        self.assertEqual(self.query("SELECT count(*) FROM webhooks"), [(1,)])

    def test_build_count_is_bounded_and_kept_across_actions(self):
        self.add_line(1, "2")
        for garbage in ("0", "-3", "abc", "1e3", "9" * 30):
            self.assertIn('data-build-count>×1<', self.client.get(self.page, query_string={"quantity": garbage}).get_data(as_text=True))
        response = self.client.post(self.page + "/build", data=dict(quantity=str(main.MAX_BUILDS + 1)), follow_redirects=True)
        self.assertIn("whole number from 1 to 1,000,000", response.get_data(as_text=True))
        self.assertEqual(self.stock(), [10, 7])
        html = self.client.get(self.page, query_string={"quantity": "4"}).get_data(as_text=True)
        self.assertIn('name="quantity" value="4"', html)
        for path, data in ((self.page + "/lines/1", dict(per_build="3")), (self.page + "/lines/1/delete", {}), (self.page, dict(item_id=1, per_build="2"))):
            self.assertEqual(self.client.post(path, data=dict(data, quantity="4")).headers["Location"], self.page + "?quantity=4")
        self.assertEqual(self.client.post(self.page + "/build", data=dict(quantity="4")).headers["Location"], self.page + "?quantity=4")
        self.assertEqual(self.client.post(self.page + "/builds/1/undo", data=dict(quantity="4")).headers["Location"], self.page + "?quantity=4")
        self.assertEqual(self.client.post(self.page + "/lines/1", data=dict(per_build="3", quantity="1")).headers["Location"], self.page)

    def test_short_plan_disables_build_and_offers_the_covered_count(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        html = self.client.get(self.page, query_string={"quantity": "3"}).get_data(as_text=True)
        self.assertIn('<button class="button primary" disabled>', html)
        self.assertIn(f'Stock covers <a href="{self.page}?quantity=2">×2</a>', html)
        html = self.client.get(self.page, query_string={"quantity": "2"}).get_data(as_text=True)
        self.assertNotIn('class="button primary" disabled', html)
        self.assertNotIn("Stock covers", html)

    def test_undo_waits_for_a_concurrent_undo_and_then_returns_nothing(self):
        self.add_line(1, "2")
        self.client.post(self.page + "/build", data=dict(quantity="1"))
        other = sqlite3.connect(main.DB_PATH)
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE builds SET undone_at='2026-01-01T00:00:00+00:00' WHERE id=1 AND undone_at IS NULL")
        results = []
        thread = threading.Thread(target=lambda: results.append(self.client.post(self.page + "/builds/1/undo", follow_redirects=True).get_data(as_text=True)))
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive())
        other.commit()
        other.close()
        thread.join(10)
        self.assertIn("already undone", results[0])
        self.assertEqual(self.stock(), [8, 7])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason LIKE 'Unbuilt%'"), [(0,)])

    def test_build_waits_for_a_concurrent_build_and_then_sees_the_shortfall(self):
        self.add_line(1, "6")
        other = sqlite3.connect(main.DB_PATH)
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE items SET quantity=quantity-6 WHERE id=1")
        results = []
        thread = threading.Thread(target=lambda: results.append(self.client.post(self.page + "/build", data=dict(quantity="1"), follow_redirects=True).get_data(as_text=True)))
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive())
        other.commit()
        other.close()
        thread.join(10)
        self.assertIn("Not enough stock to build 1", results[0])
        self.assertEqual(self.stock(), [4, 7])
        self.assertEqual(self.query("SELECT count(*) FROM builds"), [(0,)])

    def test_undo_is_all_or_nothing(self):
        self.add_line(1, "2")
        self.add_line(2, "3")
        self.client.post(self.page + "/build", data=dict(quantity="1"))
        with mock.patch.object(main, "record_movement", side_effect=[None, sqlite3.OperationalError("disk full")]):
            with self.assertLogs(main.app.logger, level="ERROR"):
                self.assertEqual(self.client.post(self.page + "/builds/1/undo").status_code, 500)
        self.assertEqual(self.stock(), [8, 4])
        self.assertEqual(self.query("SELECT undone_at FROM builds"), [(None,)])

    def test_migration_is_all_or_nothing_and_renames_legacy_webhooks(self):
        with main.app.app_context():
            conn = main.db()
            conn.executescript("""
                DROP TABLE project_items;
                CREATE TABLE project_items (project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id), quantity REAL NOT NULL, PRIMARY KEY(project_id, item_id));
                INSERT INTO project_items VALUES (1, 1, 4);
                INSERT INTO webhooks(event, destination_url, created_at) VALUES ('project.component_added', 'https://example.test/led', '2026-01-01');
            """)
            with mock.patch.object(main.json, "dumps", side_effect=ValueError("boom")):
                with self.assertRaises(ValueError):
                    main.init_db()
            conn.rollback()  # What closing the connection does when a request fails part-way.
            self.assertNotIn("per_build", {row["name"] for row in conn.execute("PRAGMA table_info(project_items)")})
            main.init_db()
            main.init_db()
        self.assertEqual(self.query("SELECT per_build FROM project_items"), [(4,)])
        self.assertEqual(self.query("SELECT count(*) FROM builds"), [(1,)])
        self.assertEqual(self.query("SELECT event FROM webhooks"), [("project.built",)])

    def test_odd_quantities_do_not_break_the_pages(self):
        self.add_line(1, "5e-324")
        self.client.post("/items/2", data=dict(quantity_change="1e999"))
        self.assertEqual(self.stock(), [10, 7])
        self.add_line(2, "1")
        for path in ("/", "/projects", self.page):
            self.assertEqual(self.client.get(path).status_code, 200)
        self.assertIn("<strong>0</strong>", self.client.get(self.page).get_data(as_text=True))
