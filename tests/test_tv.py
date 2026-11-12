"""kn0ble.com/tv channel setup: admin page, .m3u upload, public config."""
import io

import pytest

import tv_channels
from tests.conftest import sign_in_admin

M3U = """#EXTM3U
#EXTINF:-1 tvg-id="news.us" tvg-logo="https://img.example/n.png" group-title="News",Example News, East
https://cdn.example/news/index.m3u8
#EXTINF:-1 group-title="Weather",Weather 24/7
#EXTVLCOPT:http-user-agent=VLC
https://cdn.example/wx/playlist.m3u8
#EXTINF:-1,Bad line
ftp://nope.example/x
https://cdn.example/bare.m3u8
"""

HLS_MEDIA = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n"


@pytest.fixture(autouse=True)
def tv_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("TV_DATA_DIR", str(tmp_path / "tv"))


def post(client, path, **form):
    return client.post(path, data={"csrf_token": "tok", **form})


def upload(client, text, name="list.m3u", **extra):
    data = {"csrf_token": "tok", "playlist": (io.BytesIO(text.encode()), name), **extra}
    return client.post("/admin/tv/upload", data=data, content_type="multipart/form-data")


def test_parse_m3u():
    chans = tv_channels.parse_m3u("﻿" + M3U)
    assert [c["name"] for c in chans] == ["Example News, East", "Weather 24/7", "cdn.example"]
    assert chans[0]["group"] == "News"
    assert chans[0]["logo"] == "https://img.example/n.png"
    assert chans[1]["url"] == "https://cdn.example/wx/playlist.m3u8"


def test_parse_rejects_hls_manifest():
    with pytest.raises(tv_channels.NotAChannelList):
        tv_channels.parse_m3u(HLS_MEDIA)


def test_admin_required(client):
    assert "/admin/login" in client.get("/admin/tv").headers["Location"]
    resp = client.post("/admin/tv/add", data={"csrf_token": "x", "url": "https://a.example/x.m3u8"})
    assert resp.status_code in (302, 400)
    assert tv_channels.load()["channels"] == []


def test_public_config_empty_and_sessionless(client):
    resp = client.get("/tv-config.json")
    assert resp.get_json() == {"channel": None}
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in resp.headers


def test_add_and_put_on_tv(client):
    sign_in_admin(client)
    post(client, "/admin/tv/add", url="https://a.example/one.m3u8", name="One")
    post(client, "/admin/tv/add", url="https://a.example/two.m3u8", name="Two", play="1")
    cfg = client.get("/tv-config.json").get_json()["channel"]
    assert cfg["name"] == "Two" and "url" not in cfg
    html = client.get("/admin/tv").get_data(as_text=True)
    assert "On TV now: <strong>Two</strong>" in html
    assert "Channels <span class=\"muted\">(2)</span>" in html


def test_first_added_goes_on_tv(client):
    sign_in_admin(client)
    post(client, "/admin/tv/add", url="https://a.example/one.m3u8")
    assert client.get("/tv-config.json").get_json()["channel"]["name"] == "a.example"


def test_add_rejects_non_http(client):
    sign_in_admin(client)
    for bad in ("javascript:alert(1)", "ftp://x.example/a", "not a url", ""):
        post(client, "/admin/tv/add", url=bad)
    assert tv_channels.load()["channels"] == []


def test_upload_any_extension(client):
    sign_in_admin(client)
    resp = upload(client, M3U, name="mylist.3mu")
    assert resp.status_code == 302
    chans = tv_channels.load()["channels"]
    assert len(chans) == 3
    # Re-upload skips duplicates; replace starts over.
    upload(client, M3U, name="mylist.m3u")
    assert len(tv_channels.load()["channels"]) == 3
    upload(client, "#EXTM3U\n#EXTINF:-1,Solo\nhttps://s.example/s.m3u8\n", replace="1")
    data = tv_channels.load()
    assert [c["name"] for c in data["channels"]] == ["Solo"]
    assert client.get("/tv-config.json").get_json()["channel"]["name"] == "Solo"


def test_upload_hls_manifest_explains(client):
    sign_in_admin(client)
    upload(client, HLS_MEDIA, name="stream.m3u8")
    html = client.get("/admin/tv").get_data(as_text=True)
    assert "paste the stream" in html
    assert tv_channels.load()["channels"] == []


def test_upload_binary_rejected(client):
    sign_in_admin(client)
    data = {"csrf_token": "tok", "playlist": (io.BytesIO(b"\x00\x01\x02" * 50), "x.m3u")}
    client.post("/admin/tv/upload", data=data, content_type="multipart/form-data")
    assert tv_channels.load()["channels"] == []


def test_play_rename_delete_clear(client):
    sign_in_admin(client)
    upload(client, M3U)
    chans = tv_channels.load()["channels"]
    wx = chans[1]["id"]
    post(client, f"/admin/tv/{wx}/play")
    assert client.get("/tv-config.json").get_json()["channel"]["id"] == wx
    post(client, f"/admin/tv/{wx}/rename", name="  Radar  ")
    assert client.get("/tv-config.json").get_json()["channel"]["name"] == "Radar"
    post(client, f"/admin/tv/{wx}/delete")
    assert client.get("/tv-config.json").get_json() == {"channel": None}
    assert post(client, "/admin/tv/nope/play").status_code == 404
    post(client, "/admin/tv/clear")
    assert tv_channels.load()["channels"] == []


def test_names_are_escaped(client):
    sign_in_admin(client)
    upload(client, '#EXTM3U\n#EXTINF:-1,<script>x</script>\nhttps://s.example/s.m3u8\n')
    html = client.get("/admin/tv").get_data(as_text=True)
    assert "<script>x</script>" not in html


def test_public_config_hides_url(client):
    sign_in_admin(client)
    post(client, "/admin/tv/add", url="http://h.example:8080/USER/PASS/123", name="X")
    body = client.get("/tv-config.json").get_data(as_text=True)
    assert "PASS" not in body and '"name":"X"' in body.replace(" ", "")


def test_internal_config_needs_token(client, monkeypatch):
    sign_in_admin(client)
    post(client, "/admin/tv/add", url="http://h.example:8080/USER/PASS/123", name="X")
    # No env var: route is off.
    monkeypatch.delenv("TV_PROXY_TOKEN", raising=False)
    assert client.get("/tv-config-internal.json", headers={"X-TV-Token": ""}).status_code == 404
    monkeypatch.setenv("TV_PROXY_TOKEN", "s3cret")
    assert client.get("/tv-config-internal.json").status_code == 404
    assert client.get("/tv-config-internal.json", headers={"X-TV-Token": "wrong"}).status_code == 404
    resp = client.get("/tv-config-internal.json", headers={"X-TV-Token": "s3cret"})
    assert resp.get_json()["channel"]["url"] == "http://h.example:8080/USER/PASS/123"
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in resp.headers
