"""Audio files for the song buttons on kn0ble.com/oot.

Josh uploads one recording per song through the /admin/oot wizard. Files
live on the data volume (next to the SQLite database, so they survive
container updates), named <slug>.<ext>. kn0ble.com's nginx proxies
/oot/audio/ to this app's /oot-audio/, so the page and its audio share
an origin.

OOT_AUDIO_DIR overrides the folder (tests use a temp dir).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import db

# (slug, button label, group). The slug is the file name stem and the
# key in the public manifest; don't rename one without moving its file.
SONGS: list[tuple[str, str, str]] = [
    ("zeldas-lullaby", "Zelda's Lullaby", "Songs"),
    ("eponas-song", "Epona's Song", "Songs"),
    ("sarias-song", "Saria's Song", "Songs"),
    ("suns-song", "Sun's Song", "Songs"),
    ("song-of-time", "Song of Time", "Songs"),
    ("song-of-storms", "Song of Storms", "Songs"),
    ("minuet-of-forest", "Minuet of Forest", "Warp songs"),
    ("bolero-of-fire", "Bolero of Fire", "Warp songs"),
    ("serenade-of-water", "Serenade of Water", "Warp songs"),
    ("nocturne-of-shadow", "Nocturne of Shadow", "Warp songs"),
    ("requiem-of-spirit", "Requiem of Spirit", "Warp songs"),
    ("prelude-of-light", "Prelude of Light", "Warp songs"),
]
SLUGS = [s[0] for s in SONGS]
NAMES = {s[0]: s[1] for s in SONGS}

# Extension -> MIME type. These all play in current Chrome, Firefox and
# Safari except ogg/opus on older Safari, which is why mp3/m4a are the
# ones to prefer.
AUDIO_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "opus": "audio/ogg",
    "webm": "audio/webm",
    "flac": "audio/flac",
}

_FILE_RE = re.compile(r"^([a-z0-9-]+)\.([a-z0-9]+)$")


def audio_dir() -> Path:
    override = os.environ.get("OOT_AUDIO_DIR")
    path = Path(override) if override else Path(db.DB_PATH).parent / "oot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def extension_of(filename: str) -> str | None:
    """Lower-case extension if it's an allowed audio type, else None."""
    if not filename or "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower().strip()
    return ext if ext in AUDIO_TYPES else None


def file_for(slug: str) -> Path | None:
    """The stored file for a song, or None."""
    if slug not in NAMES:
        return None
    d = audio_dir()
    for ext in AUDIO_TYPES:
        p = d / f"{slug}.{ext}"
        if p.is_file():
            return p
    return None


def status() -> list[dict]:
    """Every song in order, with its file name, size and version stamp."""
    out = []
    for slug, name, group in SONGS:
        p = file_for(slug)
        entry = {"slug": slug, "name": name, "group": group, "file": None, "size": 0, "version": 0}
        if p:
            st = p.stat()
            entry.update(file=p.name, size=st.st_size, version=int(st.st_mtime))
        out.append(entry)
    return out


def save(slug: str, stream, ext: str) -> Path:
    """Write a new recording for `slug`, replacing any earlier one
    (whatever its extension). Written to a temp name first so a failed
    upload never leaves a half file where the old one was."""
    if slug not in NAMES:
        raise ValueError("unknown song")
    if ext not in AUDIO_TYPES:
        raise ValueError("unsupported audio type")
    d = audio_dir()
    tmp = d / f".{slug}.upload"
    stream.save(str(tmp)) if hasattr(stream, "save") else tmp.write_bytes(stream.read())
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise ValueError("empty file")
    delete(slug)
    final = d / f"{slug}.{ext}"
    os.replace(tmp, final)
    return final


def delete(slug: str) -> bool:
    removed = False
    if slug not in NAMES:
        return False
    d = audio_dir()
    for ext in AUDIO_TYPES:
        p = d / f"{slug}.{ext}"
        if p.is_file():
            p.unlink()
            removed = True
    return removed


def parse_served_name(name: str) -> tuple[str, str] | None:
    """('slug', 'ext') for a public file request, or None if it isn't
    one of ours. Keeps the public route from serving anything else in
    the folder."""
    m = _FILE_RE.match(name or "")
    if not m or m.group(1) not in NAMES or m.group(2) not in AUDIO_TYPES:
        return None
    return m.group(1), m.group(2)


def manifest() -> dict:
    """Public JSON for the kn0ble.com page: which songs have audio."""
    return {
        "songs": [
            {
                "slug": e["slug"],
                "name": e["name"],
                "group": e["group"],
                "file": f"{e['file']}?v={e['version']}" if e["file"] else None,
            }
            for e in status()
        ]
    }
