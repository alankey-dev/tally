import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import app.main as main


class OrderListTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path, self.previous_layout = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
        # Items 1-3 are low (10 in stock against 8 / 2.5 m minimum / 0), item 4 has no minimum, item 5 is well stocked.
        for name, quantity, minimum, part_number in (("Alpha", "1", "4", "A-1"), ("Beta wire", "0", "2.5", ""), ("Gamma", "0", "0", "G-3"), ("Delta", "5", "", "D-4"), ("Epsilon", "50", "5", "E-5")):
            self.client.post("/items/new", data=dict(name=name, quantity=quantity, minimum_quantity=minimum, part_number=part_number, unit="m" if name == "Beta wire" else "pcs", location_id=self.location))

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous_path, self.previous_layout
        self.directory.cleanup()

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def add(self, *pairs, **extra):
        data = {"item_id": [item for item, _ in pairs], **{f"quantity_{item}": quantity for item, quantity in pairs}, **extra}
        return self.client.post("/orders", data=data, follow_redirects=True).get_data(as_text=True)

    def order(self, *ids, **extra):
        return self.client.post("/orders/ordered", data=dict(entry_id=list(ids), **extra), follow_redirects=True).get_data(as_text=True)

    def entries(self):
        return self.query("SELECT item_id, quantity, status FROM order_entries ORDER BY id")

    def test_suggested_is_twice_the_minimum_less_on_hand(self):
        html = self.client.get("/orders").get_data(as_text=True)
        for name, amount in (("Alpha", "7"), ("Beta wire", "5"), ("Gamma", "1")):
            self.assertIn(f'name="quantity_{self.query("SELECT id FROM items WHERE name=?", name)[0][0]}" type="number" min="0.001" step="any" value="{amount}"', html)
        self.assertNotIn("Delta", html)
        self.assertNotIn("Epsilon", html)
        with main.app.app_context():
            self.assertEqual(main.suggested_order_quantity({"minimum_quantity": 2.5, "quantity": 1.0}), 4)
            self.assertEqual(main.suggested_order_quantity({"minimum_quantity": 5, "quantity": 5.0000000001}), 5)

    def test_ticked_rows_keep_their_own_quantity_and_adding_twice_replaces(self):
        self.add((1, "7"), (3, "4"))
        self.assertEqual(self.entries(), [(1, 7, "wanted"), (3, 4, "wanted")])
        self.assertEqual(self.query("SELECT source FROM order_entries ORDER BY id"), [("Low stock",), ("Low stock",)])
        self.add((1, "9"), (4, "2"))
        self.assertEqual(self.entries(), [(1, 9, "wanted"), (3, 4, "wanted"), (4, 2, "wanted")])
        self.assertEqual(self.query("SELECT source FROM order_entries WHERE item_id=4"), [("Added by hand",)])
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertNotIn('value="1" form="add-suggested"', html)
        self.assertIn("Beta wire", html)

    def test_invalid_quantities_and_unknown_items_write_nothing(self):
        for quantity in ("0", "-1", "nan", "abc", ""):
            self.assertIn("quantity above 0", self.add((1, "3"), (2, quantity)))
        self.assertIn("quantity above 0", self.add((1, "3"), (999, "1")))
        self.assertIn("Tick at least one", self.add())
        self.assertEqual(self.entries(), [])

    def test_entries_can_be_edited_and_removed_only_while_to_order(self):
        self.add((1, "7"), (2, "5"))
        self.client.post("/orders/1", data=dict(quantity="8"))
        self.assertEqual(self.entries()[0], (1, 8, "wanted"))
        self.assertIn("quantity above 0", self.client.post("/orders/1", data=dict(quantity="0"), follow_redirects=True).get_data(as_text=True))
        self.client.post("/orders/2/delete")
        self.assertEqual(self.entries(), [(1, 8, "wanted")])
        self.order(1)
        for path, data in (("/orders/1", dict(quantity="3")), ("/orders/1/delete", {})):
            self.assertIn("no longer waiting", self.client.post(path, data=data, follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.entries(), [(1, 8, "ordered")])

    def test_mark_ordered_records_the_order(self):
        self.add((1, "7"), (2, "5"), (3, "1"))
        self.assertIn("2 entries marked ordered", self.order(1, 2, supplier="Mouser", order_ref="PO-9", expected_on="2026-10-12"))
        self.assertEqual(self.query("SELECT status, supplier, order_ref, expected_on FROM order_entries ORDER BY id"), [("ordered", "Mouser", "PO-9", "2026-10-12")] * 2 + [("wanted", "", "", None)])
        self.assertIn("None of those entries", self.order(1, 2, supplier="Other"))
        self.assertEqual(self.query("SELECT supplier FROM order_entries WHERE id=1"), [("Mouser",)])
        self.order(3, supplier="Farnell", expected_on="soon")
        self.assertEqual(self.query("SELECT expected_on FROM order_entries WHERE id=3"), [(None,)])
        self.assertIn("Tick the entries", self.order(supplier="x"))

    def test_on_order_shows_on_the_item_and_dashboard_and_leaves_suggestions(self):
        self.add((1, "7"))
        self.order(1, supplier="Mouser", expected_on="2099-10-12")
        self.add((1, "3"))
        self.order(2, supplier="Mouser", expected_on="2020-01-20")
        html = self.client.get("/items/1").get_data(as_text=True)
        self.assertIn("On order: 10 pcs, expected 20 Jan", html)
        dashboard = self.client.get("/").get_data(as_text=True)
        self.assertIn("On order</span>", dashboard)
        self.assertIn("<strong>3</strong>", dashboard)  # Alpha, Beta and Gamma are still counted as low.
        orders = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("Late", orders)
        self.assertNotIn('name="item_id" value="1" form="add-suggested"', orders)

    def test_item_page_adds_to_the_list(self):
        self.client.post("/orders", data={"item_id": "4", "quantity_4": "1"})
        self.assertIn("Add to order list", self.client.get("/items/4").get_data(as_text=True))
        self.assertEqual(self.entries(), [(4, 1, "wanted")])

    def test_received_records_one_stock_movement(self):
        self.add((1, "7"))
        self.order(1, supplier="Mouser")
        self.assertIn("Enter the quantity", self.client.post("/orders/1/receive", data=dict(quantity="nan"), follow_redirects=True).get_data(as_text=True))
        self.assertIn("does not exist", self.client.post("/orders/99/receive", data=dict(quantity="1"), follow_redirects=True).get_data(as_text=True))
        message = self.client.post("/orders/1/receive", data=dict(quantity="6"), follow_redirects=True).get_data(as_text=True)
        self.assertIn("Received 6 pcs of Alpha. Put it in 4L 01.", message)
        self.assertEqual(self.query("SELECT quantity FROM items WHERE id=1"), [(7,)])
        self.assertEqual(self.query("SELECT quantity_change, reason FROM movements WHERE reason='Order received'"), [(6, "Order received")])
        self.assertEqual(self.query("SELECT status, received_quantity FROM order_entries"), [("received", 6)])
        self.assertIn("already received", self.client.post("/orders/1/receive", data=dict(quantity="6"), follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.query("SELECT quantity FROM items WHERE id=1"), [(7,)])

    def test_receive_waits_for_a_concurrent_receive_and_then_records_nothing(self):
        self.add((1, "7"))
        self.order(1)
        other = sqlite3.connect(main.DB_PATH)
        other.execute("BEGIN IMMEDIATE")
        other.execute("UPDATE order_entries SET status='received' WHERE id=1")
        results = []
        thread = threading.Thread(target=lambda: results.append(self.client.post("/orders/1/receive", data=dict(quantity="7"), follow_redirects=True).get_data(as_text=True)))
        thread.start()
        thread.join(0.5)
        self.assertTrue(thread.is_alive())
        other.commit()
        other.close()
        thread.join(10)
        self.assertIn("already received", results[0])
        self.assertEqual(self.query("SELECT quantity FROM items WHERE id=1"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM movements WHERE reason='Order received'"), [(0,)])

    def test_csv_export_and_the_exports_flag(self):
        self.add((1, "7"), (2, "5"))
        csv = self.client.get("/orders/export.csv").get_data(as_text=True)
        self.assertEqual(csv.splitlines()[0], "Part number,Quantity,Customer reference,Manufacturer,Component")
        self.assertIn("A-1,7.0,4L 01,,Alpha", csv)
        self.assertIn("/orders/export.csv", self.client.get("/orders").get_data(as_text=True))
        self.client.post("/settings/features", data=dict(images="1"))
        for path in ("/orders/export.csv", "/orders/export.txt"):
            self.assertEqual(self.client.get(path).status_code, 403)
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertNotIn("export.", html)
        self.assertNotIn("Mouser part list", html)
        self.assertNotIn("Order list</h2>", self.client.get("/reports").get_data(as_text=True))

    def test_mouser_text_skips_items_without_a_part_number(self):
        self.add((1, "7"), (2, "5"), (3, "1"))
        self.assertEqual(self.client.get("/orders/export.txt").get_data(as_text=True), "A-1|7\nG-3|1\n")
        html = self.client.get("/orders").get_data(as_text=True)
        self.assertIn("A-1|7", html)
        self.assertIn("Left out, no part number: Beta wire", html)
        self.assertEqual(self.client.get("/orders/export.pdf").status_code, 404)

    def test_init_db_twice_leaves_one_table_and_index(self):
        with main.app.app_context():
            main.init_db()
            main.init_db()
        self.assertEqual(self.query("SELECT count(*) FROM sqlite_master WHERE name IN ('order_entries', 'order_entries_one_wanted')"), [(2,)])

    def test_project_shortages_are_added_once_and_less_what_is_on_order(self):
        page = self.client.post("/projects", data=dict(title="Blinky")).headers["Location"]
        self.client.post(page, data=dict(item_id=4, per_build="2"))  # 5 on hand
        self.client.post(page, data=dict(item_id=5, per_build="20"))  # 50 on hand
        with mock.patch.object(main, "dispatch_webhooks") as webhooks:
            message = self.client.post(page + "/order-shortages", data=dict(quantity="3"), follow_redirects=True).get_data(as_text=True)
        self.assertIn("2 items added to the order list", message)
        self.assertEqual(self.entries(), [(4, 1, "wanted"), (5, 10, "wanted")])
        self.assertEqual(self.query("SELECT source FROM order_entries"), [("Blinky ×3",)] * 2)
        self.assertEqual(webhooks.call_args.args[0], "order.added")
        self.assertEqual([entry["quantity"] for entry in webhooks.call_args.args[1]["entries"]], [1, 10])
        self.client.post(page + "/order-shortages", data=dict(quantity="3"))
        self.assertEqual(self.entries(), [(4, 1, "wanted"), (5, 10, "wanted")])
        self.order(1, 2, supplier="Mouser")
        self.assertIn("Nothing to add for ×3", self.client.post(page + "/order-shortages", data=dict(quantity="3"), follow_redirects=True).get_data(as_text=True))
        self.assertIn("Nothing to add for ×1", self.client.post(page + "/order-shortages", data=dict(quantity="1"), follow_redirects=True).get_data(as_text=True))
        self.assertEqual(self.entries(), [(4, 1, "ordered"), (5, 10, "ordered")])
        self.assertIn("data-order-shortages", self.client.get(page).get_data(as_text=True))

    def test_order_added_webhook_is_accepted_and_sent(self):
        self.assertIn("Webhook destination added", self.client.post("/settings/webhooks", data=dict(event="order.added", destination_url="https://example.test/hook"), follow_redirects=True).get_data(as_text=True))
        with mock.patch.object(main, "dispatch_webhooks") as webhooks:
            self.add((1, "7"), (2, "5"))
        event, payload = webhooks.call_args.args
        self.assertEqual(event, "order.added")
        self.assertEqual([entry["quantity"] for entry in payload["entries"]], [7, 5])
        self.assertEqual(payload["entries"][0]["location"], "4L 01")


if __name__ == "__main__":
    unittest.main()
