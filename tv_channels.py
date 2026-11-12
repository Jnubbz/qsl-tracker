"""Channels for kn0ble.com/tv.

Josh manages them at /admin/tv: paste a stream URL, or upload an IPTV
.m3u playlist (one or many channels). One channel is "on TV"; the
public /tv-config.json says which, and kn0ble.com/tv plays it.

Stored as one JSON file on the data volume, next to the SQLite
database, so it survives container updates. TV_DATA_DIR overrides the
folder (tests use a temp dir).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from urllib.parse import urlparse

import db

MAX_CHANNELS = 5000
MAX_NAME = 120
MAX_URL = 2048

_lock = threading.Lock()


def data_dir() -> Path:
    override = os.environ.get("TV_DATA_DIR")
    path = Path(override) if override else Path(db.DB_PATH).parent / "tv"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path() -> Path:
    return data_dir() / "channels.json"


def load() -> dict:
    """{"channels": [{"id", "name", "url", "group", "logo"}], "active": id|None}"""
    try:
        data = json.loads(_path().read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {"channels": [], "active": None}
    chans = [c for c in data.get("channels", []) if isinstance(c, dict) and c.get("id") and c.get("url")]
    active = data.get("active")
    if active not in {c["id"] for c in chans}:
        active = None
    return {"channels": chans, "active": active}


def _save(data: dict) -> None:
    d = data_dir()
    tmp = d / ".channels.json.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    os.replace(tmp, _path())


def clean_url(url: str) -> str | None:
    """The URL stripped, if it's an http(s) URL with a host; else None."""
    url = (url or "").strip()
    if not url or len(url) > MAX_URL or any(c in url for c in "\r\n\t "):
        return None
    try:
        parts = urlparse(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return url


def _clean_text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", (value or "")).strip()[:limit]


def _new_id() -> str:
    return secrets.token_hex(4)


# --- .m3u parsing ----------------------------------------------------

_ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_HLS_TAGS = ("#EXT-X-TARGETDURATION", "#EXT-X-STREAM-INF", "#EXT-X-MEDIA-SEQUENCE")


class NotAChannelList(ValueError):
    """The file is an HLS stream manifest, not an IPTV channel list."""


def parse_m3u(text: str) -> list[dict]:
    """Channels from an IPTV .m3u/.m3u8 playlist.

    Each #EXTINF line names the URL on the next non-comment line; the
    name is the text after the last top-level comma, and tvg-logo /
    group-title attributes are kept. Bare URL lines with no #EXTINF
    become channels named after their host. An HLS manifest (segments
    or variant streams) raises NotAChannelList: it is the stream itself
    and only plays from its own server, so its URL is what to paste.
    """
    text = text.lstrip("﻿")
    upper = text.upper()
    if any(tag in upper for tag in _HLS_TAGS):
        raise NotAChannelList("that file is a stream, not a channel list")
    out: list[dict] = []
    pending: dict | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("#EXTINF"):
            info = line.split(":", 1)[1] if ":" in line else ""
            attrs = dict(_ATTR_RE.findall(info))
            # Name = after the comma that follows the attributes.
            no_attrs = _ATTR_RE.sub("", info)
            name = no_attrs.split(",", 1)[1] if "," in no_attrs else ""
            pending = {
                "name": _clean_text(name or attrs.get("tvg-name", ""), MAX_NAME),
                "group": _clean_text(attrs.get("group-title", ""), 60),
                "logo": clean_url(attrs.get("tvg-logo", "")) or "",
            }
            continue
        if line.startswith("#"):
            continue
        url = clean_url(line)
        if url:
            ch = pending or {"name": "", "group": "", "logo": ""}
            ch["url"] = url
            if not ch["name"]:
                ch["name"] = urlparse(url).netloc
            out.append(ch)
        pending = None
    return out


# --- changes -----------------------------------------------------------

def add(name: str, url: str, group: str = "", logo: str = "") -> dict:
    url = clean_url(url)
    if not url:
        raise ValueError("that isn't an http(s) URL")
    with _lock:
        data = load()
        for c in data["channels"]:
            if c["url"] == url:
                if name:
                    c["name"] = _clean_text(name, MAX_NAME)
                    _save(data)
                return c
        if len(data["channels"]) >= MAX_CHANNELS:
            raise ValueError(f"there are already {MAX_CHANNELS} channels")
        ch = {
            "id": _new_id(),
            "name": _clean_text(name, MAX_NAME) or urlparse(url).netloc,
            "url": url,
            "group": _clean_text(group, 60),
            "logo": clean_url(logo) or "",
        }
        data["channels"].append(ch)
        if data["active"] is None:
            data["active"] = ch["id"]
        _save(data)
        return ch


def add_many(channels: list[dict], replace: bool = False) -> tuple[int, int]:
    """Add parsed channels; skip URLs already present. Returns
    (added, skipped). With replace=True the old list is dropped first."""
    with _lock:
        data = {"channels": [], "active": None} if replace else load()
        seen = {c["url"] for c in data["channels"]}
        added = skipped = 0
        for ch in channels:
            if ch["url"] in seen or len(data["channels"]) >= MAX_CHANNELS:
                skipped += 1
                continue
            seen.add(ch["url"])
            data["channels"].append({"id": _new_id(), **{k: ch.get(k, "") for k in ("name", "url", "group", "logo")}})
            added += 1
        if data["active"] is None and data["channels"]:
            data["active"] = data["channels"][0]["id"]
        _save(data)
        return added, skipped


def set_active(channel_id: str) -> dict | None:
    with _lock:
        data = load()
        ch = next((c for c in data["channels"] if c["id"] == channel_id), None)
        if ch:
            data["active"] = ch["id"]
            _save(data)
        return ch


def rename(channel_id: str, name: str) -> bool:
    name = _clean_text(name, MAX_NAME)
    if not name:
        return False
    with _lock:
        data = load()
        for c in data["channels"]:
            if c["id"] == channel_id:
                c["name"] = name
                _save(data)
                return True
        return False


def delete(channel_id: str) -> bool:
    with _lock:
        data = load()
        before = len(data["channels"])
        data["channels"] = [c for c in data["channels"] if c["id"] != channel_id]
        if len(data["channels"]) == before:
            return False
        if data["active"] == channel_id:
            data["active"] = None
        _save(data)
        return True


def clear() -> None:
    with _lock:
        _save({"channels": [], "active": None})


def active_channel() -> dict | None:
    data = load()
    return next((c for c in data["channels"] if c["id"] == data["active"]), None)


def public_config() -> dict:
    """What kn0ble.com/tv needs: the one channel on TV, or null."""
    ch = active_channel()
    if not ch:
        return {"channel": None}
    return {"channel": {"id": ch["id"], "name": ch["name"], "url": ch["url"]}}
