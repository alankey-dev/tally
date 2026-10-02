import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from unittest import mock
from pathlib import Path

import app.main as main

PDF = b"%PDF-1.4\n%test\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = main.DB_PATH, main.LAYOUT, main.UPLOAD_DIR
        main.DB_PATH = Path(self.directory.name) / "test.db"
        main.UPLOAD_DIR = Path(self.directory.name) / "uploads"
        main.LAYOUT = "example"
        self.client = main.app.test_client()
        self.client.get("/")
        with main.app.app_context():
            self.location = main.db().execute("SELECT id FROM locations WHERE code='4L 01'").fetchone()[0]
            self.entries = [row[0] for row in main.db().execute("SELECT id FROM catalogue ORDER BY id LIMIT 3")]
        self.plain = self.add_item("Plain part")

    def tearDown(self):
        main.DB_PATH, main.LAYOUT, main.UPLOAD_DIR = self.previous
        self.directory.cleanup()

    def add_item(self, name, entry=None):
        path = "/items/new" + (f"?catalogue={entry}" if entry else "")
        if entry:
            name = self.query("SELECT name FROM catalogue WHERE id=?", entry)[0][0]
        self.client.post(path, data=dict(name=name, quantity="1", unit="pcs", location_id=self.location))
        return self.query("SELECT max(id) FROM items")[0][0]

    def query(self, sql, *args):
        with main.app.app_context():
            return [tuple(row) for row in main.db().execute(sql, args)]

    def files(self):
        return sorted(path.name for path in main.UPLOAD_DIR.glob("*")) if main.UPLOAD_DIR.exists() else []

    def attach(self, item_id=None, content=None, filename="sheet.pdf", mimetype="application/pdf", **data):
        if content is not None:
            data["file"] = (io.BytesIO(content), filename, mimetype)
        return self.client.post(f"/items/{item_id or self.plain}/attachments", data=data, content_type="multipart/form-data", follow_redirects=True).get_data(as_text=True)

    def rows(self):
        return self.query("SELECT item_id, catalogue_id, label, file_path, url FROM attachments ORDER BY id")

    def test_pdf_upload_is_served_with_safe_headers(self):
        html = self.attach(content=PDF, label="Datasheet: LM358")
        self.assertIn("Attachment added.", html)
        (item_id, _, label, name, _), = self.rows()
        self.assertEqual((item_id, label, self.files()), (self.plain, "Datasheet: LM358", [name]))
        self.assertIn(f"/uploads/{name}", html)
        response = self.client.get(f"/uploads/{name}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertNotIn("Content-Security-Policy", response.headers)
        self.assertIn("inline", response.headers["Content-Disposition"])
        self.assertIn("Datasheet", response.headers["Content-Disposition"])
        self.assertIn(".pdf", response.headers["Content-Disposition"])
        self.attach(content=PDF, mimetype="application/octet-stream", label="Octet")
        self.assertTrue(self.rows()[1][3].endswith(".pdf"))

    def test_other_files_are_sandboxed_and_images_still_take_svg(self):
        self.attach(content=PNG, filename="pins.png", mimetype="image/png", label="Pinout")
        name = self.rows()[0][3]
        response = self.client.get(f"/uploads/{name}")
        self.assertEqual((response.headers["Content-Security-Policy"], response.headers["X-Content-Type-Options"]), ("sandbox", "nosniff"))
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        self.client.post("/items/new", data=dict(name="With image", quantity="1", location_id=self.location, image=(io.BytesIO(svg), "a.svg", "image/svg+xml")), content_type="multipart/form-data")
        self.assertEqual(len(self.files()), 2)

    def test_rejected_files_leave_nothing(self):
        cases = [(b"<html></html>", "a.html", "text/html"), (b"<svg/>", "a.svg", "image/svg+xml"), (b"\0\0\0\x18ftypheic", "a.heic", "image/heic"), (b"not a pdf", "a.pdf", "application/pdf")]
        for content, filename, mimetype in cases:
            html = self.attach(content=content, filename=filename, mimetype=mimetype, label="Bad")
            self.assertIn("Use a PDF, PNG, JPEG, WebP or GIF", html)
        self.assertIn("pick the photo from Photos", html)
        self.assertEqual((self.rows(), self.files()), ([], []))

    def test_size_cap_applies_only_to_attachments(self):
        with mock.patch.object(main, "MAX_ATTACHMENT_BYTES", 100):
            html = self.attach(content=PDF + b"0" * 500, label="Big")
            self.assertIn("Files can be up to 20 MB.", html)
            self.assertEqual((self.rows(), self.files()), ([], []))
            png = PNG + b"0" * 500
            self.client.post("/items/new", data=dict(name="Photo", quantity="1", location_id=self.location, image=(io.BytesIO(png), "a.png", "image/png")), content_type="multipart/form-data")
        self.assertEqual(len(self.files()), 1)

    def test_links(self):
        self.assertIn("Attachment added.", self.attach(url="https://example.test/ds.pdf", label="Online"))
        self.assertEqual(self.rows(), [(self.plain, None, "Online", "", "https://example.test/ds.pdf")])
        for bad in ("javascript:alert(1)", "ftp://example.test/a", "https://user:pw@example.test/a"):
            self.assertIn("Enter a valid http(s) link.", self.attach(url=bad, label="Bad"))
        self.assertEqual(len(self.rows()), 1)

    def test_defaults_and_both_given(self):
        self.attach(content=PDF, filename="lm358.pdf")
        self.attach(url="https://example.test/x")
        self.attach(url="https://example.test/y", label="L" * 200)
        self.assertEqual([row[2] for row in self.rows()], ["lm358", "Datasheet", "L" * 120])
        self.assertIn("Choose a file or a link, not both.", self.attach(content=PDF, url="https://example.test/x"))
        self.assertIn("Choose a file or a link.", self.attach(label="Nothing"))
        self.assertEqual(len(self.rows()), 3)

    def test_removal(self):
        other = self.add_item("Other")
        self.attach(content=PDF, label="Mine")
        self.attach(item_id=other, url="https://example.test/o", label="Theirs")
        mine, theirs = [row for row in self.query("SELECT id FROM attachments ORDER BY id")]
        self.assertEqual(self.client.post(f"/items/{self.plain}/attachments/{theirs[0]}/delete").status_code, 404)
        self.assertEqual(len(self.rows()), 2)
        before = self.client.get("/api/version").json["version"]
        self.client.post(f"/items/{self.plain}/attachments/{mine[0]}/delete")
        self.assertEqual(([row[2] for row in self.rows()], self.files()), (["Theirs"], []))
        self.assertNotEqual(self.client.get("/api/version").json["version"], before)
        html = self.client.post(f"/items/{self.plain}/attachments/{mine[0]}/delete", follow_redirects=True).get_data(as_text=True)
        self.assertIn("That attachment was already removed.", html)

    def test_version_changes_on_add(self):
        before = self.client.get("/api/version").json["version"]
        self.attach(url="https://example.test/x")
        self.assertNotEqual(self.client.get("/api/version").json["version"], before)

    def test_feature_switch(self):
        with main.app.app_context():
            main.db().execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('feature.edit_components', '0')")
            main.db().commit()
        self.assertEqual(self.client.post(f"/items/{self.plain}/attachments", data=dict(url="https://example.test/x")).status_code, 403)
        self.assertEqual(self.client.post(f"/items/{self.plain}/attachments/1/delete").status_code, 403)
        self.assertNotIn("/attachments", self.client.get(f"/items/{self.plain}").get_data(as_text=True))

    def test_sharing_through_a_catalogue_entry(self):
        first, second = self.add_item("", self.entries[0]), self.add_item("", self.entries[0])
        unrelated = self.add_item("", self.entries[1])
        self.assertEqual(self.query("SELECT catalogue_id FROM items WHERE id=?", first), [(self.entries[0],)])
        self.attach(item_id=first, url="https://example.test/shared", label="Shared sheet", share="1")
        self.assertEqual(self.rows(), [(None, self.entries[0], "Shared sheet", "", "https://example.test/shared")])
        for item_id, shown in ((first, True), (second, True), (unrelated, False)):
            self.assertEqual("Shared sheet" in self.client.get(f"/items/{item_id}").get_data(as_text=True), shown)
        self.assertIn("Remove from all 2 items", self.client.get(f"/items/{second}").get_data(as_text=True))
        self.attach(url="https://example.test/forged", label="Forged", share="1")
        self.assertEqual(self.rows()[1][:2], (self.plain, None))
        self.client.post(f"/items/{second}/attachments/1/delete")
        self.assertEqual([row[2] for row in self.rows()], ["Forged"])

    def test_catalogue_replace_keeps_ids_and_copies_shared_files_down(self):
        keep, drop, empty = self.entries
        a, b = self.add_item("", keep), self.add_item("", drop)
        c = self.add_item("", drop)
        d = self.add_item("", empty)
        self.attach(item_id=b, content=PDF, label="Sheet", share="1")
        entry = {"name": self.query("SELECT name FROM catalogue WHERE id=?", keep)[0][0], "family": "generic", "manufacturer": "", "part_number": "", "attributes": {}}
        with main.app.app_context():
            with main.db() as conn:
                main.save_catalogue(conn, [entry], replace=True)
        self.assertEqual(self.query("SELECT id FROM catalogue"), [(keep,)])
        self.assertEqual(self.query("SELECT catalogue_id FROM items WHERE id IN (?, ?, ?, ?) ORDER BY id", a, b, c, d), [(keep,), (None,), (None,), (None,)])
        rows = self.rows()
        self.assertEqual(sorted(row[0] for row in rows), [b, c])
        self.assertEqual(len({row[3] for row in rows}), 1)
        self.assertEqual(len(self.files()), 1)
        first = self.query("SELECT id FROM attachments ORDER BY id")[0][0]
        self.client.post(f"/items/{b}/attachments/{first}/delete")
        self.assertEqual((len(self.rows()), len(self.files())), (1, 1))

    def test_migration_adds_catalogue_link_once(self):
        with main.app.app_context():
            conn = main.db()
            name = conn.execute("SELECT name FROM catalogue WHERE id=?", (self.entries[0],)).fetchone()[0]
            conn.executescript("""
                DROP TABLE attachments;
                ALTER TABLE items DROP COLUMN catalogue_id;
            """)
            conn.execute("UPDATE items SET name=? WHERE id=?", (name.upper(), self.plain))
            conn.execute("DELETE FROM settings WHERE key='catalogue.seeded'")
            conn.execute("INSERT INTO webhooks(event, destination_url, created_at) VALUES ('project.component_added', 'https://example.test/h', 'now')")
            conn.commit()
            main.init_db()
            main.init_db()
        self.assertEqual(self.query("SELECT catalogue_id FROM items WHERE id=?", self.plain), [(self.entries[0],)])
        self.assertEqual(self.query("SELECT event FROM webhooks"), [("project.built",)])
        self.assertEqual(self.query("SELECT count(*) FROM attachments"), [(0,)])

    def test_full_backup_zip(self):
        self.attach(content=PDF, label="Sheet")
        name = self.rows()[0][3]
        self.add_item("Written just before")
        response = self.client.get("/settings/backup.zip")
        self.assertEqual((response.status_code, response.mimetype), (200, "application/zip"))
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as bundle:
            self.assertIn(f"uploads/{name}", bundle.namelist())
            copy = Path(self.directory.name) / "copy.db"
            copy.write_bytes(bundle.read("tally-backup.sqlite3"))
        self.assertEqual(sqlite3.connect(copy).execute("SELECT count(*) FROM items WHERE name='Written just before'").fetchone(), (1,))
        response.close()
        self.assertEqual([path.name for path in main.DB_PATH.parent.glob("tmp*")], [])

    def test_components_list_marks_items_with_attachments(self):
        shared_a, shared_b = self.add_item("", self.entries[0]), self.add_item("", self.entries[0])
        self.attach(item_id=shared_a, url="https://example.test/s", share="1")
        self.attach(item_id=self.plain, url="https://example.test/p")
        self.add_item("Bare part")
        for view in ("tiles", "list"):
            html = self.client.get("/components", query_string={"view": view}).get_data(as_text=True)
            self.assertEqual(html.count("Has attachments"), 3)
        html = self.client.get("/components?q=Bare").get_data(as_text=True)
        self.assertNotIn("Has attachments", html)
