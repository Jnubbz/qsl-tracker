"""Card requests: optional mailing address, admin list, and notification email."""
import sqlite3

import db
import mailer

from tests.conftest import sign_in_admin


def test_request_stores_optional_address_and_email(client):
    resp = client.post(
        "/request-qsl",
        data={
            "callsign": "w1aw",
            "note": "20m CW",
            "email": "op@example.com",
            "address": "225 Main Street\nNewington, CT 06111\nUSA",
        },
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/request-qsl/thanks")
    rows = db.list_qsl_requests()
    assert len(rows) == 1
    assert rows[0]["callsign"] == "W1AW"
    assert rows[0]["note"] == "20m CW"
    assert rows[0]["contact_email"] == "op@example.com"
    assert rows[0]["address"] == "225 Main Street\nNewington, CT 06111\nUSA"


def test_request_without_address_stores_blank(client):
    client.post("/request-qsl", data={"callsign": "K1JT"})
    rows = db.list_qsl_requests()
    assert rows[0]["callsign"] == "K1JT"
    assert rows[0]["address"] == ""
    assert rows[0]["contact_email"] == ""


def test_address_is_capped_at_500_characters(client):
    client.post("/request-qsl", data={"callsign": "W1AW", "address": "x" * 800})
    assert len(db.list_qsl_requests()[0]["address"]) == 500


def test_both_honeypot_names_drop_the_request(client):
    for field in ("website", "hp_url"):
        resp = client.post(
            "/request-qsl",
            data={"callsign": "W1AW", "address": "1 Main St", field: "https://spam.example"},
        )
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/request-qsl/thanks")
    assert db.list_qsl_requests() == []


def test_admin_request_view_shows_address(client):
    client.post(
        "/request-qsl",
        data={
            "callsign": "W1AW",
            "email": "op@example.com",
            "address": "225 Main Street\nNewington, CT 06111",
            "note": "please QSL",
        },
    )
    denied = client.get("/admin/requests")
    assert denied.status_code == 302
    assert "/admin/login" in denied.headers["Location"]

    sign_in_admin(client)
    resp = client.get("/admin/requests")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<h1>Card requests</h1>" in html
    assert "W1AW" in html
    assert "225 Main Street" in html
    assert "Newington, CT 06111" in html
    assert "op@example.com" in html
    assert "so you can reach them" in html
    assert "reply" not in html.lower()


def test_notification_includes_address_and_does_not_set_reply_to(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("NOTIFY_EMAIL", "josh@example.com")
    captured = {}

    class Resp:
        status_code = 200
        text = ""

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["json"] = json
        return Resp()

    monkeypatch.setattr(mailer.requests, "post", fake_post)
    ok = mailer.send_qsl_request_email(
        "W1AW",
        "20m",
        "op@example.com",
        "225 Main Street\nNewington, CT 06111\nUSA",
    )
    assert ok is True
    payload = captured["json"]
    assert "reply_to" not in payload
    assert "Reply-To" not in payload
    assert payload["to"] == ["josh@example.com"]
    assert "225 Main Street" in payload["text"]
    assert "Newington, CT 06111" in payload["text"]
    assert "Email (so you can reach them): op@example.com" in payload["text"]
    assert "op@example.com" not in payload.get("reply_to", "")


def test_existing_database_gains_address_column(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE qsl_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            callsign TEXT NOT NULL,
            note TEXT,
            contact_email TEXT,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO qsl_requests (session_id, callsign, note, contact_email, created_at) VALUES (?,?,?,?,?)",
        ("old", "W1AW", "earlier", "old@example.com", 1),
    )
    conn.commit()
    conn.close()

    db.init_db()
    db.save_qsl_request("new", "K1JT", "hi", "new@example.com", "1 Main St\nSpringfield")
    rows = {row["callsign"]: row for row in db.list_qsl_requests()}
    assert rows["K1JT"]["address"] == "1 Main St\nSpringfield"
    assert rows["W1AW"]["address"] in (None, "")
    assert rows["W1AW"]["contact_email"] == "old@example.com"
