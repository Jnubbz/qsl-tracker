"""Container probe: unauthenticated, and it does not touch S3 or QRZ."""
from __future__ import annotations

import unittest

from .support import app, appmod, db, s3


class HealthzTests(unittest.TestCase):
    def test_healthz_returns_ok_without_s3_or_qrz(self):
        def fail(*_args, **_kwargs):
            raise AssertionError("healthz touched a service it should not")

        originals = {
            "purge_expired": db.purge_expired,
            "get_auth": db.get_auth,
            "get_conn": db.get_conn,
            "get_json": s3.get_json,
            "get_json_meta": s3.get_json_meta,
            "get_object_bytes": s3.get_object_bytes,
            "lookup_callsign_raw": appmod.lookup_callsign_raw,
            "get_session_key": appmod.get_session_key,
            "fetch_log_page": appmod.fetch_log_page,
            "fetch_status": appmod.fetch_status,
            "lookup_location": appmod.lookup_location,
        }
        db.purge_expired = fail
        db.get_auth = fail
        db.get_conn = fail
        s3.get_json = fail
        s3.get_json_meta = fail
        s3.get_object_bytes = fail
        appmod.lookup_callsign_raw = fail
        appmod.get_session_key = fail
        appmod.fetch_log_page = fail
        appmod.fetch_status = fail
        appmod.lookup_location = fail
        try:
            client = app.test_client()
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True), "ok")
            self.assertEqual(response.mimetype, "text/plain")
            self.assertNotIn("Set-Cookie", response.headers)
        finally:
            db.purge_expired = originals["purge_expired"]
            db.get_auth = originals["get_auth"]
            db.get_conn = originals["get_conn"]
            s3.get_json = originals["get_json"]
            s3.get_json_meta = originals["get_json_meta"]
            s3.get_object_bytes = originals["get_object_bytes"]
            appmod.lookup_callsign_raw = originals["lookup_callsign_raw"]
            appmod.get_session_key = originals["get_session_key"]
            appmod.fetch_log_page = originals["fetch_log_page"]
            appmod.fetch_status = originals["fetch_status"]
            appmod.lookup_location = originals["lookup_location"]


if __name__ == "__main__":
    unittest.main()
