"""The one-time Gmail import of old card requests."""
import importlib.util
from pathlib import Path

import db

spec = importlib.util.spec_from_file_location(
    "import_card_requests", Path(__file__).resolve().parent.parent / "tools" / "import_card_requests.py"
)
imp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imp)


def test_import_adds_all_answered_and_is_rerunnable(client):
    # JA0IAA's request was already saved on Unraid seconds before its email.
    db.save_qsl_request("live", "JA0IAA", "", "", "")
    with db.get_conn() as conn:
        conn.execute("UPDATE qsl_requests SET created_at = ?", (imp._ts("2026-11-01T01:28:30Z"),))

    added, skipped = imp.run()
    assert (added, skipped) == (len(imp.REQUESTS) - 1, 1)
    rows = db.list_qsl_requests()
    assert len(rows) == len(imp.REQUESTS)
    imported = [r for r in rows if r["callsign"] != "JA0IAA"]
    assert all(r["answered_at"] for r in imported)
    db5pj = next(r for r in rows if r["callsign"] == "DB5PJ")
    assert db5pj["contact_email"] == "DB5PJ@gmx.de"
    assert db5pj["created_at"] == imp._ts("2026-10-27T16:38:05Z")
    assert sum(r["callsign"] == "DL9RMH" for r in rows) == 2

    assert imp.run() == (0, len(imp.REQUESTS))
    assert len(db.list_qsl_requests()) == len(imp.REQUESTS)


def test_imported_requests_show_on_the_admin_page(client):
    from tests.conftest import sign_in_admin

    imp.run()
    sign_in_admin(client)
    html = client.get("/admin/requests").get_data(as_text=True)
    assert "All answered" in html
    assert "HIROSHI SADA" in html
    assert "KN0BLE" not in html.split("<h1>Card requests</h1>")[1].split("</section>")[0]
