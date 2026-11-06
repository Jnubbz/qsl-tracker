"""Song buttons for kn0ble.com/oot: admin wizard + public audio routes."""
import io

import pytest

import oot_songs
from tests.conftest import sign_in_admin

MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\xff\xfb" * 400


@pytest.fixture(autouse=True)
def audio_dir(tmp_path, monkeypatch):
    d = tmp_path / "oot"
    monkeypatch.setenv("OOT_AUDIO_DIR", str(d))
    return d


def upload(client, slug, data=MP3, name="take1.mp3", **extra):
    form = {"csrf_token": "tok", "audio": (io.BytesIO(data), name), **extra}
    return client.post(f"/admin/oot/{slug}", data=form, content_type="multipart/form-data")


def test_wizard_requires_admin(client):
    resp = client.get("/admin/oot")
    assert resp.status_code == 302
    assert "/admin/login" in resp.headers["Location"]
    resp = client.post("/admin/oot/song-of-time", data={"csrf_token": "x"})
    assert resp.status_code in (302, 400)
    assert oot_songs.file_for("song-of-time") is None


def test_wizard_starts_on_first_song_and_lists_all(client):
    sign_in_admin(client)
    html = client.get("/admin/oot").get_data(as_text=True)
    assert "Step 1 of 12" in html
    assert "Zelda&#39;s Lullaby" in html
    for _slug, name, _g in oot_songs.SONGS:
        assert name.replace("'", "&#39;") in html
    assert "0 of 12 songs have a recording" in html


def test_upload_saves_and_advances(client, audio_dir):
    sign_in_admin(client)
    resp = upload(client, "zeldas-lullaby")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/oot?step=1")
    assert (audio_dir / "zeldas-lullaby.mp3").read_bytes() == MP3
    html = client.get("/admin/oot").get_data(as_text=True)
    # No ?step: opens on the first song still missing.
    assert "Step 2 of 12" in html
    assert "1 of 12 songs have a recording" in html


def test_stay_keeps_same_step(client):
    sign_in_admin(client)
    resp = upload(client, "suns-song", stay="1")
    assert resp.headers["Location"].endswith("/admin/oot?step=3")


def test_last_song_goes_to_done(client):
    sign_in_admin(client)
    resp = upload(client, "prelude-of-light")
    assert "done=1" in resp.headers["Location"]
    html = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "All the way through" in html


def test_replace_with_other_format_removes_old(client, audio_dir):
    sign_in_admin(client)
    upload(client, "song-of-storms")
    upload(client, "song-of-storms", data=b"\x00\x00\x00\x18ftypM4A " + b"x" * 50, name="new.M4A")
    assert not (audio_dir / "song-of-storms.mp3").exists()
    assert (audio_dir / "song-of-storms.m4a").exists()


def test_rejects_non_audio_and_empty_and_unknown(client, audio_dir):
    sign_in_admin(client)
    resp = upload(client, "song-of-time", data=b"<script>", name="evil.html")
    assert resp.status_code == 302
    assert oot_songs.file_for("song-of-time") is None
    upload(client, "song-of-time", data=b"", name="blank.mp3")
    assert oot_songs.file_for("song-of-time") is None
    assert not any(audio_dir.glob(".*upload"))
    assert upload(client, "not-a-song").status_code == 404
    assert upload(client, "..%2f..%2fetc").status_code == 404


def test_missing_file_field(client):
    sign_in_admin(client)
    resp = client.post("/admin/oot/song-of-time", data={"csrf_token": "tok"})
    assert resp.status_code == 302


def test_upload_needs_csrf(client):
    sign_in_admin(client)
    resp = client.post(
        "/admin/oot/song-of-time",
        data={"audio": (io.BytesIO(MP3), "a.mp3")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert oot_songs.file_for("song-of-time") is None


def test_delete(client, audio_dir):
    sign_in_admin(client)
    upload(client, "eponas-song")
    resp = client.post("/admin/oot/eponas-song/delete", data={"csrf_token": "tok"})
    assert resp.headers["Location"].endswith("/admin/oot?step=1")
    assert oot_songs.file_for("eponas-song") is None


def test_manifest_and_file_are_public_without_cookie(client):
    sign_in_admin(client)
    upload(client, "sarias-song")
    anon = client.application.test_client()
    resp = anon.get("/oot-audio/manifest.json")
    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert "Set-Cookie" not in resp.headers
    songs = {s["slug"]: s for s in resp.get_json()["songs"]}
    assert len(songs) == 12
    assert songs["song-of-time"]["file"] is None
    f = songs["sarias-song"]["file"]
    assert f.startswith("sarias-song.mp3?v=")
    audio = anon.get("/oot-audio/" + f)
    assert audio.status_code == 200
    assert audio.mimetype == "audio/mpeg"
    assert audio.data == MP3
    assert "Set-Cookie" not in audio.headers


def test_file_supports_range_requests(client):
    sign_in_admin(client)
    upload(client, "sarias-song")
    resp = client.application.test_client().get(
        "/oot-audio/sarias-song.mp3", headers={"Range": "bytes=0-9"}
    )
    assert resp.status_code == 206
    assert resp.data == MP3[:10]


@pytest.mark.parametrize("name", ["song-of-time.mp3", "nope.mp3", "sarias-song.txt", ".sarias-song.upload", "..", "manifest"])
def test_public_file_404s(client, name):
    assert client.get("/oot-audio/" + name).status_code == 404
