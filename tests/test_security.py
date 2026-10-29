"""Security fixes: image route, redirects, password compare, CSRF, cookies, upload cap."""
from __future__ import annotations

import unittest
from io import BytesIO
from pathlib import Path

from werkzeug.datastructures import FileStorage

from .support import app, appmod, client_with_token, s3

HEX = "a" * 32


class SecurityTests(unittest.TestCase):
    def test_image_key_allowlist(self):
        self.assertFalse(s3.is_card_image_key("photocards/_index.json"))
        self.assertFalse(s3.is_card_image_key("photocards/_INDEX.JSON"))
        self.assertFalse(s3.is_card_image_key(f"photocards/K1ABC/{HEX}.html"))
        self.assertFalse(s3.is_card_image_key(f"photocards/K1ABC/{HEX}.svg"))
        self.assertFalse(s3.is_card_image_key("photocards/../secret.txt"))
        self.assertFalse(s3.is_card_image_key(f"photocards/K1ABC/..//{HEX}.jpg"))
        self.assertTrue(s3.is_card_image_key(f"photocards/K1ABC/{HEX}.jpg"))
        self.assertTrue(s3.is_card_image_key(f"photocards/K1ABC/{HEX}.JPG"))
        self.assertTrue(s3.is_card_image_key(f"photocards/FP/KJ1V/{HEX}.png"))

    def test_index_is_not_downloadable_and_photos_are(self):
        calls = []
        original = appmod.get_object_bytes

        def fake(key):
            calls.append(key)
            return b"img-bytes", "text/html"

        appmod.get_object_bytes = fake
        client = app.test_client()
        try:
            blocked = client.get("/photomap/image/photocards/_index.json")
            self.assertEqual(blocked.status_code, 404)
            self.assertEqual(calls, [])
            self.assertNotIn(b"img-bytes", blocked.data)

            html = client.get(f"/photomap/image/photocards/K1ABC/{HEX}.html")
            self.assertEqual(html.status_code, 404)

            ok = client.get(f"/photomap/image/photocards/FP/KJ1V/{HEX}.jpg")
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(ok.data, b"img-bytes")
            # Served as an image even if S3 stored a different type.
            self.assertEqual(ok.mimetype, "image/jpeg")
            self.assertEqual(ok.headers.get("X-Content-Type-Options"), "nosniff")
        finally:
            appmod.get_object_bytes = original

    def test_delete_refuses_the_index(self):
        with self.assertRaises(s3.S3Error):
            s3.delete_object("photocards/_index.json")

    def test_upload_rejects_non_images(self):
        upload = FileStorage(stream=BytesIO(b"<html></html>"), filename="card.html", content_type="text/html")
        with self.assertRaises(s3.S3Error):
            s3.upload_card_image(upload, "K1ABC")

    def test_open_redirect_and_backslash_bypass(self):
        client = client_with_token()
        for evil in ("https://evil.example/", "//evil.example", "/\\evil.example", "/\\\\evil.example"):
            response = client.post(
                "/admin/login",
                data={"password": "secret", "csrf_token": "tok", "next": evil},
            )
            self.assertEqual(response.status_code, 302, evil)
            location = response.headers["Location"]
            self.assertNotIn("evil.example", location)
            self.assertTrue(location.endswith("/admin/qso-label"), location)

        # A same-app path is still honored.
        response = client.post(
            "/admin/login",
            data={"password": "secret", "csrf_token": "tok", "next": "/admin/log"},
        )
        self.assertTrue(response.headers["Location"].endswith("/admin/log"))

        # Wrong password re-renders without echoing the off-site target.
        response = client.post(
            "/admin/login",
            data={"password": "nope", "csrf_token": "tok", "next": "https://evil.example/phish"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"evil.example", response.data)

        # _safe_next (batch / sync forms) rejects the same bypass.
        old_key = appmod.QRZ_LOGBOOK_API_KEY
        appmod.QRZ_LOGBOOK_API_KEY = ""
        try:
            admin = client_with_token(is_admin=True)
            response = admin.post(
                "/admin/log/sync",
                data={"csrf_token": "tok", "next": "/\\evil.example"},
            )
            self.assertEqual(response.status_code, 302)
            location = response.headers["Location"]
            self.assertNotIn("evil", location)
            with app.test_request_context():
                self.assertTrue(location.endswith(appmod.url_for("admin_log")))
        finally:
            appmod.QRZ_LOGBOOK_API_KEY = old_key

    def test_non_ascii_password_does_not_500(self):
        client = client_with_token()
        response = client.post(
            "/admin/login",
            data={"password": "páß", "csrf_token": "tok"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Wrong password", response.data)

        original = appmod.ADMIN_PASSWORD
        appmod.ADMIN_PASSWORD = "sécret"
        try:
            response = client.post(
                "/admin/login",
                data={"password": "sécret", "csrf_token": "tok", "next": "/admin/log"},
            )
            self.assertEqual(response.status_code, 302)
            with client.session_transaction() as sess:
                self.assertTrue(sess.get("is_admin"))
        finally:
            appmod.ADMIN_PASSWORD = original

    def test_csrf_required_except_request_qsl(self):
        client = app.test_client()
        missing = client.post("/login", data={"qrz_username": "a", "qrz_password": "b"})
        self.assertEqual(missing.status_code, 400)

        client = client_with_token()
        bad = client.post("/admin/logout", data={"csrf_token": "wrong"})
        self.assertEqual(bad.status_code, 400)

        # The embedded kn0ble.com form has no token. It must still submit.
        public = app.test_client()
        ok = public.post("/request-qsl", data={"callsign": "K1ABC"})
        self.assertEqual(ok.status_code, 302)
        self.assertIn("/request-qsl/thanks", ok.headers["Location"])

    def test_post_forms_include_the_token(self):
        root = Path(__file__).resolve().parents[1] / "templates"
        for path in root.glob("*.html"):
            text = path.read_text()
            forms = text.lower().count('method="post"')
            if not forms:
                continue
            includes = text.count('{% include "_csrf.html" %}')
            self.assertEqual(forms, includes, path.name)

    def test_session_cookie_flags(self):
        original = app.config["SESSION_COOKIE_SECURE"]
        try:
            app.config["SESSION_COOKIE_SECURE"] = True
            response = app.test_client().get("/request-qsl")
            cookie = response.headers.get("Set-Cookie", "")
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)
            self.assertIn("Secure", cookie)
            self.assertTrue(appmod._env_flag("NOT_A_REAL_VAR", True))
            self.assertFalse(appmod._env_flag("SESSION_COOKIE_SECURE", True))
        finally:
            app.config["SESSION_COOKIE_SECURE"] = original

    def test_upload_size_is_capped(self):
        original = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 100
        try:
            response = app.test_client().post(
                "/request-qsl",
                data={"callsign": "K1ABC", "note": "x" * 500},
                follow_redirects=True,
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"too large", response.data.lower())
        finally:
            app.config["MAX_CONTENT_LENGTH"] = original


if __name__ == "__main__":
    unittest.main()
