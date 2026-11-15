"""Load the TV channel list from a playlist link (the provider's m3u URL).

Big playlists (tens of MB) uploaded through the browser crawl over a home
upload link and tie up qsl-tracker's single worker until gunicorn kills
it. Here the server downloads the list itself, in a background thread, so
the request returns at once and the site stays responsive. The link is
saved (it usually holds the account login, so it is only ever shown
masked) and "Refresh" re-pulls it.

State lives in <tv data dir>/playlist_source.json.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

import tv_channels

log = logging.getLogger(__name__)

MAX_BYTES = 300 * 1024 * 1024
TIMEOUT = 60  # seconds without data before giving up
USER_AGENT = "VLC/3.0.20 LibVLC/3.0.20"  # same as the relay; IPTV servers expect a player

_lock = threading.Lock()
_job: dict = {"running": False}


def _path():
    return tv_channels.data_dir() / "playlist_source.json"


def load_source() -> dict:
    try:
        return json.loads(_path().read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_source(data: dict) -> None:
    tmp = tv_channels.data_dir() / ".playlist_source.json.tmp"
    tmp.write_text(json.dumps(data), "utf-8")
    os.replace(tmp, _path())


def masked(url: str) -> str:
    """scheme://host:port/… — enough to recognise, no login details."""
    if not url:
        return ""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc.rsplit('@', 1)[-1]}/…"


def status() -> dict:
    """For the admin page: saved link (masked), last result, live progress."""
    src = load_source()
    with _lock:
        job = dict(_job)
    return {
        "has_url": bool(src.get("url")),
        "url_masked": masked(src.get("url", "")),
        "last": src.get("last"),  # {"at", "ok", "message"}
        "running": job.get("running", False),
        "bytes": job.get("bytes", 0),
        "started": job.get("started"),
        "phase": job.get("phase", ""),
    }


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    chunks, total = [], 0
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError(f"the playlist is bigger than {MAX_BYTES // (1024 * 1024)} MB")
            chunks.append(chunk)
            with _lock:
                _job["bytes"] = total
    raw = b"".join(chunks)
    if b"\x00" in raw[:4096]:
        raise ValueError("that link didn't return a playlist (it isn't text)")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _run(url: str, replace: bool) -> None:
    ok, message = False, ""
    try:
        text = _download(url)
        with _lock:
            _job["phase"] = "reading channels"
        try:
            channels = tv_channels.parse_m3u(text)
        except tv_channels.NotAChannelList:
            raise ValueError("that link is a single stream, not a channel list; add it under Add a stream")
        if not channels:
            raise ValueError("no channels found at that link")
        with _lock:
            _job["phase"] = "saving"
        added, dupes, over = tv_channels.add_many(channels, replace=replace)
        message = f"Added {added:,} channel{'s' if added != 1 else ''}."
        if dupes:
            message += f" {dupes:,} were already in the list."
        if over:
            message += (f" {over:,} didn't fit: the limit is {tv_channels.MAX_CHANNELS:,}, and live"
                        " channels were kept first, so it's movies/series that were left out.")
        ok = True
    except urllib.error.HTTPError as exc:
        message = f"The provider answered {exc.code} {exc.reason}."
    except Exception as exc:  # network, size, parse
        message = f"Couldn't load it: {exc}."
    log.info("playlist link load: %s", message)
    src = load_source()
    src["last"] = {"at": int(time.time()), "ok": ok, "message": message}
    _save_source(src)
    with _lock:
        _job.clear()
        _job["running"] = False


def start(url: str | None = None, replace: bool = True) -> bool:
    """Save `url` (if given) and start loading it in the background.
    Returns False if a load is already running. Raises ValueError for a
    bad URL or when no link is saved."""
    if url is not None:
        url = tv_channels.clean_url(url)
        if not url:
            raise ValueError("that isn't an http(s) link")
    with _lock:
        if _job.get("running"):
            return False
        src = load_source()
        if url:
            src["url"] = url
            _save_source(src)
        url = src.get("url")
        if not url:
            raise ValueError("no playlist link is saved yet")
        _job.clear()
        _job.update(running=True, bytes=0, started=int(time.time()), phase="downloading")
    threading.Thread(target=_run, args=(url, replace), daemon=True).start()
    return True


def forget() -> None:
    """Remove the saved link (the channels stay)."""
    src = load_source()
    src.pop("url", None)
    _save_source(src)
