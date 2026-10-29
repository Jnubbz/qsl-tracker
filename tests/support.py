"""Test harness. Imported before the app so env vars are in place.

Forces dummy credentials and a throwaway SQLite file, then refuses any
outbound socket so a test cannot reach AWS, QRZ, or Resend.
"""
from __future__ import annotations

import os
import socket
import tempfile

os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["ADMIN_PASSWORD"] = "secret"
os.environ["S3_BUCKET"] = "test-bucket"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["SESSION_COOKIE_SECURE"] = "0"
os.environ["QRZ_LOGBOOK_API_KEY"] = "test-logbook-key"
os.environ["QSL_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="qsl-test-"), "qsl.db")
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("NOTIFY_EMAIL", None)

import app as appmod  # noqa: E402
import db  # noqa: E402
import s3  # noqa: E402
from app import app  # noqa: E402

_orig_connect = socket.socket.connect


def _blocked_connect(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if host in ("127.0.0.1", "::1", "localhost"):
        return _orig_connect(self, address)
    raise RuntimeError(f"refusing network connection to {address}")


socket.socket.connect = _blocked_connect


def client_with_token(is_admin: bool = False):
    """A test client whose session already holds a CSRF token."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["session_id"] = "test-session"
        sess["_csrf_token"] = "tok"
        if is_admin:
            sess["is_admin"] = True
    db.save_auth("test-session", "qrz-key", "tester")
    return client
