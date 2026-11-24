import pytest

"""Navigation, naming, and Logbook section routing.

Sync, login, and password checks are intentionally not exercised here.
"""
from html.parser import HTMLParser

import photomap_store

import app as app_module
from db import get_auth

from tests.conftest import nav_html, sign_in_admin, sign_in_qrz

PUBLIC_LINKS = ("ADIF Address Extractor", "QSL Photo Map", "Request a Card", "Admin")
ADMIN_LINKS = (
    "Logbook",
    "QSO Labels",
    "QSO Map",
    "ADIF Address Extractor",
    "QSL Cards",
    "Card requests",
    "QSL Photo Map",
    "Request a Card",
)


class _ButtonForms(HTMLParser):
    """Pair each submit button with the form that would receive it."""

    def __init__(self):
        super().__init__()
        self.by_id = {}
        self.buttons = []
        self._stack = []
        self._capture = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form":
            form = {
                "action": attr.get("action") or "",
                "method": (attr.get("method") or "get").lower(),
                "id": attr.get("id") or "",
            }
            if form["id"]:
                self.by_id[form["id"]] = form
            self._stack.append(form)
        elif tag == "button":
            self._capture = {
                "type": (attr.get("type") or "submit").lower(),
                "form_id": attr.get("form") or "",
                "form": self._stack[-1] if self._stack else None,
                "hook": bool(
                    attr.get("id") or attr.get("class") or attr.get("data-key") or attr.get("data-src")
                ),
            }
            self._buf = []

    def handle_endtag(self, tag):
        if tag == "form" and self._stack:
            self._stack.pop()
        elif tag == "button" and self._capture is not None:
            self._capture["label"] = " ".join("".join(self._buf).split())
            self.buttons.append(self._capture)
            self._capture = None

    def handle_data(self, data):
        if self._capture is not None:
            self._buf.append(data)


def _assert_submit_buttons_post(html: str):
    """Every submit control posts (or gets) a real form. A button with no
    form, and a type=button with nothing for script to bind, does nothing."""
    parser = _ButtonForms()
    parser.feed(html)
    assert parser.buttons, "page has no buttons"
    for button in parser.buttons:
        if button["type"] == "button":
            assert button["hook"], f"button {button['label']!r} has no script hook"
            continue
        assert button["type"] == "submit", button
        form = button["form"]
        if button["form_id"]:
            form = parser.by_id.get(button["form_id"])
            assert form is not None, f"{button['label']!r} names a missing form"
        assert form is not None, f"{button['label']!r} is not in a form"
        assert form["action"], f"{button['label']!r} form has no action"
        if form["method"] == "post":
            assert "csrf_token" in html


def test_public_nav_and_address_labels_name(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    nav = nav_html(html)
    for label in PUBLIC_LINKS:
        assert label in nav
    for label in ("Logbook", "QSO Labels", "QSO Map", "QSL Cards"):
        assert label not in nav
    assert "Raw Address Ripper" not in html
    assert "Address Labels" not in html
    assert "<h1>ADIF Address Extractor</h1>" in html
    assert 'aria-current="page"' in nav
    assert "ADIF Address Extractor" in nav.split('aria-current="page"')[1][:80]


def test_public_pages_keep_the_same_nav(client):
    for path in ("/photomap", "/request-qsl", "/request-qsl/thanks"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        nav = nav_html(resp.get_data(as_text=True))
        assert "Request a Card" in nav
        assert "Logbook" not in nav
        assert "QSO Labels" not in nav


def test_request_a_card_name(client):
    resp = client.get("/request-qsl")
    html = resp.get_data(as_text=True)
    assert "<h1>Request a Card</h1>" in html
    assert ">Request a Card</button>" in html
    assert "Request a QSL Card" not in html
    assert 'name="address"' in html
    assert "Only so I can reach you if I need to." in html


def test_qrz_header_logout_is_separate_from_admin(client):
    sign_in_qrz(client)
    resp = client.get("/photomap")
    html = resp.get_data(as_text=True)
    assert "QRZ: <strong>KN0BLE</strong>" in html
    assert 'action="/logout"' in html
    assert "Log out of QRZ" in html
    assert "Log out of admin" not in html
    assert "Logbook" not in nav_html(html)


def test_index_still_redirects_when_qrz_logged_in(client):
    sign_in_qrz(client)
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/raw-address")


def test_address_labels_page_for_qrz_user(client):
    sign_in_qrz(client)
    resp = client.get("/raw-address")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<h1>ADIF Address Extractor</h1>" in html
    assert "Raw Address Ripper" not in html
    assert "Address Labels" not in html
    assert "Extract addresses from this log" in html


def test_admin_nav(client):
    sign_in_admin(client)
    resp = client.get("/request-qsl")
    html = resp.get_data(as_text=True)
    nav = nav_html(html)
    for label in ADMIN_LINKS:
        assert label in nav
    assert 'href="/admin/log"' in nav
    assert 'href="/admin/photomap/import-adif"' not in nav
    assert ">Admin</a>" not in nav
    assert 'action="/admin/logout"' in html
    assert "Log out of admin" in html
    assert "Log out (admin)" not in html


@pytest.mark.parametrize(
    "path",
    ["/dashboard", "/export.csv", "/admin/label", "/admin/raw-address"],
)
def test_retired_address_routes_still_redirect(client, path):
    resp = client.get(path)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/raw-address")


def test_admin_qsos_redirect_preserves_filters(client):
    sign_in_admin(client)
    resp = client.get("/admin/qsos?entity=France&callsign=F5")
    assert resp.status_code == 302
    loc = resp.headers["Location"]
    assert "/admin/qso-label" in loc
    assert "entity=France" in loc
    assert "callsign=F5" in loc


def test_admin_qsos_requires_admin(client):
    resp = client.get("/admin/qsos")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]


def _mock_log(monkeypatch, qrz_total=12, qso_count=10, dupes=None):
    monkeypatch.setattr(app_module, "QRZ_LOGBOOK_API_KEY", "test-key")
    monkeypatch.setattr(photomap_store, "count_my_qsos", lambda: qso_count)
    monkeypatch.setattr(photomap_store, "newest_qso_date", lambda: "20260115")
    monkeypatch.setattr(
        photomap_store,
        "qrz_sync_state",
        lambda: {
            "last_sync": 1_700_000_000,
            "caught_up": True,
            "qrz_total": qrz_total,
            "qrz_dupe_logids": dupes or [],
        },
    )


def test_logbook_sections_and_not_matched_link(client, monkeypatch):
    sign_in_admin(client)
    _mock_log(monkeypatch, dupes=["9"])

    sync = client.get("/admin/log")
    assert sync.status_code == 200
    sync_html = sync.get_data(as_text=True)
    assert "<h1>Logbook</h1>" in sync_html
    assert 'id="log-panel-sync"' in sync_html
    assert 'id="log-panel-missing"' in sync_html
    assert 'id="log-panel-import"' in sync_html
    assert 'id="log-not-matched"' in sync_html
    assert 'href="/admin/log?section=missing#log-panel-missing"' in sync_html
    assert "not matched" in sync_html
    assert "Your log" not in sync_html
    assert "section-tabs" not in sync_html
    # One of each action. Sync posts; the tab that used the same label is gone.
    assert sync_html.count(">Sync from QRZ</button>") == 1
    assert sync_html.count(">Sync from QRZ</a>") == 0
    assert sync_html.count(">Find missing QSOs</button>") == 1
    assert sync_html.count(">Find missing QSOs</a>") == 0
    assert sync_html.count(">Import</button>") == 1
    assert sync_html.count(">Re-sync everything from the start</button>") == 1
    assert 'name="full" value="1"' in sync_html
    assert 'action="/admin/log/sync"' in sync_html
    assert 'action="/admin/log/missing"' in sync_html
    assert 'name="adif_file"' in sync_html
    assert 'href="/admin/log?section=sync"' not in sync_html
    _assert_submit_buttons_post(sync_html)

    missing = client.get("/admin/log?section=missing")
    missing_html = missing.get_data(as_text=True)
    assert 'id="log-panel-missing"' in missing_html
    assert 'class="card is-target"' in missing_html
    assert ">Find missing QSOs</button>" in missing_html
    assert 'name="adif_file"' in missing_html
    assert missing_html.count(">Sync from QRZ</button>") == 1
    assert missing_html.count(">Find missing QSOs</button>") == 1
    _assert_submit_buttons_post(missing_html)

    imported = client.get("/admin/log?section=import")
    import_html = imported.get_data(as_text=True)
    assert 'id="log-panel-import"' in import_html
    assert "<h2>Import ADIF</h2>" in import_html
    assert 'name="adif_file"' in import_html
    assert import_html.count(">Find missing QSOs</button>") == 1
    assert import_html.count(">Sync from QRZ</button>") == 1
    assert import_html.count(">Import</button>") == 1
    _assert_submit_buttons_post(import_html)

    bogus = client.get("/admin/log?section=nope")
    assert 'id="log-panel-sync"' in bogus.get_data(as_text=True)


def test_old_import_url_opens_import_section(client, monkeypatch):
    sign_in_admin(client)
    _mock_log(monkeypatch)
    resp = client.get("/admin/photomap/import-adif")
    assert resp.status_code == 200
    assert 'id="log-panel-import"' in resp.get_data(as_text=True)


def test_adif_import_post_returns_to_import_section(client):
    sign_in_admin(client)
    resp = client.post("/admin/log", data={"csrf_token": "tok"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/log?section=import")


def test_logbook_requires_admin_and_missing_stays_post_only(client):
    resp = client.get("/admin/log")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
    missing = client.get("/admin/log/missing")
    assert missing.status_code == 405


def test_qso_labels_naming(client, monkeypatch):
    sign_in_admin(client)
    monkeypatch.setattr(photomap_store, "list_dxcc_entities", lambda: [])
    monkeypatch.setattr(photomap_store, "list_all_my_qsos", lambda **kwargs: [])
    monkeypatch.setattr(photomap_store, "count_my_qsos", lambda: 0)
    monkeypatch.setattr(photomap_store, "qrz_sync_state", lambda: {})
    monkeypatch.setattr(
        photomap_store, "card_print_settings", lambda: {"nudge_x": 0.0, "nudge_y": 0.0}
    )
    resp = client.get("/admin/qso-label")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "<h1>QSO Labels</h1>" in html
    assert "ADIF Address Extractor" in html
    assert "Raw Address Ripper" not in html
    assert "Address Labels" not in html
    assert "Your log" not in html
    assert "Pick from the map" not in html
    assert "Logbook" in html
    assert "Log out (admin)" not in html
    # The label page keeps its own sync, and that one posts back here.
    assert html.count(">Sync from QRZ</button>") == 1
    assert 'action="/admin/log/sync"' in html
    assert 'name="next"' in html
    _assert_submit_buttons_post(html)


def test_qsl_cards_pages(client, monkeypatch):
    sign_in_admin(client)
    monkeypatch.setattr(photomap_store, "list_all_photo_cards", lambda: [])
    monkeypatch.setattr(photomap_store, "list_recent_photo_cards", lambda limit=10: [])
    monkeypatch.setattr(
        photomap_store,
        "get_photo_card",
        lambda card_id: {
            "id": card_id,
            "callsign": "W1AW",
            "qso_date": "20260101",
            "band": "20M",
            "mode": "CW",
            "freq": "14.025",
            "rst_sent": "599",
            "rst_rcvd": "599",
            "note": "Field Day",
        },
    )
    monkeypatch.setattr(photomap_store, "get_images_for_card", lambda card_id: [])

    manage = client.get("/admin/photomap/manage")
    manage_html = manage.get_data(as_text=True)
    assert manage.status_code == 200
    assert "<h1>QSL Cards</h1>" in manage_html
    assert "Your log" not in manage_html
    assert manage_html.count('href="/admin/photomap/upload"') == 1
    assert ">Upload a card</a>" in manage_html
    assert "section-tabs" not in manage_html
    assert ">All cards</a>" not in manage_html

    upload = client.get("/admin/photomap/upload")
    upload_html = upload.get_data(as_text=True)
    assert "<h1>Upload a card</h1>" in upload_html
    assert 'name="qso_date"' in upload_html
    assert 'name="rst_rcvd"' in upload_html
    assert "Open the Logbook" in upload_html
    assert "section-tabs" not in upload_html
    assert ">Upload</a>" not in upload_html
    assert 'aria-label="Breadcrumb"' in upload_html
    assert ">Save card</button>" in upload_html
    _assert_submit_buttons_post(upload_html)

    edit = client.get("/admin/photomap/edit/3")
    edit_html = edit.get_data(as_text=True)
    assert edit.status_code == 200
    assert "Edit W1AW" in edit_html
    assert 'value="20260101"' in edit_html
    assert 'value="Field Day"' in edit_html
    assert 'aria-label="Breadcrumb"' in edit_html
    assert "section-tabs" not in edit_html
    assert ">All cards</a>" not in edit_html
    assert ">Save changes</button>" in edit_html
    _assert_submit_buttons_post(edit_html)


def test_logout_routes_unchanged(client):
    sign_in_qrz(client, session_id="s1")
    resp = client.post("/logout", data={"csrf_token": "tok"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert get_auth("s1") is None

    sign_in_qrz(client, session_id="s2")
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["session_id"] = "s2"
        sess["_csrf_token"] = "tok"
    resp = client.post("/admin/logout", data={"csrf_token": "tok"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/")
    assert get_auth("s2") is not None
