"""Test harness. Imported before the app so env vars are in place.

Forces dummy credentials and a throwaway SQLite file, then refuses any
outbound socket so a test cannot reach AWS, QRZ, or Resend.
"""
from __future__ import annotations

import json
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


class MemoryBucket:
    """In-memory stand-in for the one JSON object in S3.

    Honors If-Match / If-None-Match the way put_json does, so tests can
    force a conflict and check the retry instead of talking to AWS.
    """

    def __init__(self):
        self.objects = {}
        self.seq = 0
        self.conflicts_remaining = 0
        self._orig = None

    def reset(self):
        self.objects.clear()
        self.seq = 0
        self.conflicts_remaining = 0

    def get_json(self, key):
        data, _etag = self.get_json_meta(key)
        return data

    def get_json_meta(self, key):
        obj = self.objects.get(key)
        if obj is None:
            return None, None
        return json.loads(obj["body"]), obj["etag"]

    def put_json(self, key, data, if_match=None, if_none_match=None):
        if self.conflicts_remaining:
            self.conflicts_remaining -= 1
            raise s3.S3Conflict("forced conflict")
        current = self.objects.get(key)
        if if_match is not None and (current is None or current["etag"] != if_match):
            raise s3.S3Conflict("etag mismatch")
        if if_none_match == "*" and current is not None:
            raise s3.S3Conflict("already exists")
        self.seq += 1
        etag = f'"{self.seq}"'
        self.objects[key] = {"body": json.dumps(data), "etag": etag}
        return etag

    def install(self):
        if self._orig is None:
            self._orig = (s3.get_json, s3.get_json_meta, s3.put_json)
        s3.get_json = self.get_json
        s3.get_json_meta = self.get_json_meta
        s3.put_json = self.put_json

    def uninstall(self):
        if self._orig is not None:
            s3.get_json, s3.get_json_meta, s3.put_json = self._orig
            self._orig = None


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
