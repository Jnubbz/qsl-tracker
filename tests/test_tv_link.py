"""TV playlist loaded from a link, in the background."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import tv_channels
import tv_fetch
from tests.conftest import sign_in_admin

PLAYLIST = (
    "#EXTM3U\n"
    '#EXTINF:-1 tvg-id="n.us" group-title="News",News One\nhttp://prov.example:8080/USER/PASS/1\n'
    '#EXTINF:-1 group-title="Movies",A Film\nhttp://prov.example:8080/movie/USER/PASS/2.mkv\n'
)


class Prov(BaseHTTPRequestHandler):
    body = PLAYLIST.encode()
    status = 200
    seen_ua = None

    def do_GET(self):
        Prov.seen_ua = self.headers.get("User-Agent")
        self.send_response(Prov.status)
        self.end_headers()
        if Prov.status == 200:
            self.wfile.write(Prov.body)

    def log_message(self, *a):
        pass


@pytest.fixture(autouse=True)
def tv_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TV_DATA_DIR", str(tmp_path / "tv"))


@pytest.fixture
def prov():
    Prov.body, Prov.status = PLAYLIST.encode(), 200
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Prov)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/get.php?username=USER&password=PASS&type=m3u_plus"
    srv.shutdown()


def wait_done(secs=10):
    end = time.time() + secs
    while time.time() < end:
        if not tv_fetch.status()["running"]:
            return tv_fetch.status()
        time.sleep(0.05)
    raise AssertionError("still running")


def post(client, **form):
    return client.post("/admin/tv/link", data={"csrf_token": "tok", **form})


def test_load_from_link_and_mask(client, prov):
    sign_in_admin(client)
    r = post(client, url=prov, replace="1")
    assert r.status_code == 302
    st = wait_done()
    assert st["last"]["ok"] and "Added 2 channels." in st["last"]["message"]
    assert Prov.seen_ua.startswith("VLC/")
    assert [c["name"] for c in tv_channels.load()["channels"]] == ["News One", "A Film"]
    assert tv_channels.active_channel()["name"] == "News One"
    html = client.get("/admin/tv").get_data(as_text=True)
    assert "Saved link: http://127.0.0.1:" in html and "Refresh from link" in html
    assert "username=USER" not in html and "password=PASS" not in html  # saved link never shown in full
    assert "USER" not in html.split("Saved link:")[1].split("</p>")[0]
    assert "Added 2 channels." in html
    js = client.get("/admin/tv/link/status.json").get_json()
    assert js["has_url"] and "PASS" not in str(js)


def test_refresh_replaces(client, prov):
    sign_in_admin(client)
    post(client, url=prov, replace="1"); wait_done()
    Prov.body = b'#EXTM3U\n#EXTINF:-1,Only\nhttp://prov.example/x/y/9\n'
    post(client, refresh="1", replace="1"); wait_done()
    assert [c["name"] for c in tv_channels.load()["channels"]] == ["Only"]
    post(client, forget="1")
    assert not tv_fetch.status()["has_url"]
    assert [c["name"] for c in tv_channels.load()["channels"]] == ["Only"]  # channels stay
    r = post(client, refresh="1", replace="1")
    assert "no playlist link is saved yet" in client.get("/admin/tv").get_data(as_text=True)


def test_errors_are_reported(client, prov):
    sign_in_admin(client)
    Prov.status = 403
    post(client, url=prov); st = wait_done()
    assert not st["last"]["ok"] and "403" in st["last"]["message"]
    Prov.status = 200
    Prov.body = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nseg.ts\n"
    post(client, url=prov); st = wait_done()
    assert "single stream" in st["last"]["message"]
    Prov.body = b"\x00\x01binary"
    post(client, url=prov); st = wait_done()
    assert "isn't text" in st["last"]["message"]
    post(client, url="javascript:alert(1)")
    assert "isn&#39;t an http(s) link" in client.get("/admin/tv").get_data(as_text=True)


def test_needs_admin(client, prov):
    assert "/admin/login" in client.post("/admin/tv/link", data={"csrf_token": "x", "url": prov}).headers.get("Location", "/admin/login")
    assert client.get("/admin/tv/link/status.json").status_code in (302, 401, 403)
    assert not tv_fetch.status()["has_url"]
