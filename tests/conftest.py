# support sets test env vars and blocks outbound sockets before app import.
import tests.support  # noqa: F401

import db
import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "qsl.db")
    db.init_db()
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    return app_module.app.test_client()


def sign_in_admin(client, session_id="admin-session"):
    with client.session_transaction() as sess:
        sess["is_admin"] = True
        sess["session_id"] = session_id
        sess["_csrf_token"] = "tok"


def sign_in_qrz(client, username="KN0BLE", session_id="qrz-session"):
    db.save_auth(session_id, "test-key", username)
    with client.session_transaction() as sess:
        sess["session_id"] = session_id
        sess["_csrf_token"] = "tok"


def nav_html(html: str) -> str:
    start = html.index('<nav class="site-nav"')
    end = html.index("</nav>", start)
    return html[start:end]
