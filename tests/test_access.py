import tempfile
import unittest
from pathlib import Path

import app.main as main


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_path = main.DB_PATH
        main.DB_PATH = Path(self.directory.name) / "test.db"
        self.client = main.app.test_client()
        self.client.post("/settings/access", data={"password": "correct horse battery"})
        self.client.post("/settings/auth", data={"auth_enabled": "1"})

    def tearDown(self):
        main.DB_PATH = self.previous_path
        self.directory.cleanup()

    def test_sign_out_locks_the_app(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("Sign out", self.client.get("/").get_data(as_text=True))
        self.client.post("/logout")
        self.assertIn("/access", self.client.get("/").location)
        self.assertIn("/access", self.client.get("/settings/backup").location)
        self.assertIn("/access", main.app.test_client().get("/items").location)

    def test_sign_in(self):
        self.client.post("/logout")
        self.assertEqual(self.client.post("/access", data={"password": "wrong password"}).status_code, 200)
        self.assertIn("/access", self.client.get("/").location)
        response = self.client.post("/access?next=/items", data={"password": "correct horse battery"})
        self.assertEqual(response.location, "/items")
        self.assertEqual(self.client.get("/").status_code, 200)

    def test_next_cannot_leave_the_site(self):
        self.client.post("/logout")
        for target in ("https://example.com", "//example.com"):
            response = self.client.post("/access", query_string={"next": target}, data={"password": "correct horse battery"})
            self.assertEqual(response.location, "/")

    def test_cookie_does_not_carry_the_password_hash(self):
        with main.app.app_context():
            password_hash = main.db().execute("SELECT value FROM settings WHERE key='access.password_hash'").fetchone()[0]
        with self.client.session_transaction() as session:
            self.assertNotEqual(session["access_unlocked"], password_hash)

    def test_changing_the_password_signs_out_other_browsers(self):
        other = main.app.test_client()
        other.post("/access", data={"password": "correct horse battery"})
        self.assertEqual(other.get("/").status_code, 200)
        self.client.post("/settings/access", data={"password": "a different password"})
        self.assertIn("/access", other.get("/").location)


if __name__ == "__main__":
    unittest.main()
