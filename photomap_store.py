"""
QSL Photo Map metadata store -- backed by a single JSON object in S3
instead of the app's SQLite database.

Why: Render's free-tier disk (where instance/qsl_tracker.db lives) is
ephemeral and gets wiped on every redeploy and every spin-down after
inactivity. That's fine for the rest of the app -- visitor sessions and
QRZ lookups are meant to be short-lived -- but it defeats the entire
point of the QSL Photo Map, which is durably keeping Josh's uploaded
cards. The photo *files* were already safe in S3 (see s3.py); this
module moves the *records* (which callsign has which photos, at what
location, from what QSO) into S3 too, so the whole feature is actually
durable on Render's free plan -- no paid disk, no change of host needed.

Stored at photocards/_index.json -- deliberately inside the same
photocards/ prefix the existing IAM policy already grants PutObject/
GetObject on, so this needed zero AWS console changes on Josh's end.

Simple by design: the whole store is read, mutated, and rewritten on
every write call (and just read on every read call). That's wasteful
compared to a real database -- no partial updates, no transactions,
last-write-wins if two writes ever raced -- but this is a low-traffic,
single-admin tool (Josh uploading his own QSL cards occasionally), not
a high-concurrency system. The extra S3 round trips cost nothing anyone
will notice here, and it keeps the code straightforward.
"""
from __future__ import annotations

import time

from s3 import S3Error, get_json, put_json

INDEX_KEY = "photocards/_index.json"


def _empty() -> dict:
    # A fresh dict every call -- never share/mutate a module-level
    # constant here, or every "empty" store would end up aliased.
    return {
        "callsign_locations": {},
        "photo_cards": [],
        "my_qsos": [],
        "next_photo_card_id": 1,
        "next_my_qso_id": 1,
    }


def _load() -> dict:
    data = get_json(INDEX_KEY)
    if data is None:
        return _empty()
    # Tolerate an older/partial blob gaining new keys over time.
    base = _empty()
    base.update(data)
    return base


def _save(data: dict) -> None:
    put_json(INDEX_KEY, data)


# ---------------------------------------------------------------------
# Callsign locations (cached QRZ lookups: country/state/county/grid/lat/lon)
# ---------------------------------------------------------------------

def get_callsign_location(callsign: str) -> dict | None:
    data = _load()
    return data["callsign_locations"].get(callsign.upper())


def save_callsign_location(loc) -> None:
    """`loc` is a qrz.QrzLocation. Cached indefinitely -- re-fetch by
    removing the callsign from the JSON blob if a station's QRZ info
    ever needs refreshing."""
    data = _load()
    data["callsign_locations"][loc.callsign.upper()] = {
        "callsign": loc.callsign.upper(),
        "country": loc.country,
        "state": loc.state,
        "county": loc.county,
        "grid": loc.grid,
        "lat": loc.lat,
        "lon": loc.lon,
        "looked_up_at": time.time(),
    }
    _save(data)


# ---------------------------------------------------------------------
# Photo cards -- one entry per upload ("this card, from this QSO"). A
# callsign can have several (repeat contacts, multiple cards); each
# entry carries its own list of S3 image keys directly (no separate
# join table needed now that this isn't relational storage).
# ---------------------------------------------------------------------

def add_photo_card(
    callsign: str, qso_date: str, band: str, mode: str, freq: str,
    rst_sent: str, rst_rcvd: str, note: str,
) -> int:
    data = _load()
    card_id = data["next_photo_card_id"]
    data["next_photo_card_id"] = card_id + 1
    data["photo_cards"].append({
        "id": card_id,
        "callsign": callsign.upper(),
        "qso_date": qso_date,
        "band": band,
        "mode": mode,
        "freq": freq,
        "rst_sent": rst_sent,
        "rst_rcvd": rst_rcvd,
        "note": note,
        "created_at": time.time(),
        "images": [],
    })
    _save(data)
    return card_id


def add_photo_card_image(photo_card_id: int, s3_key: str) -> None:
    data = _load()
    for card in data["photo_cards"]:
        if card["id"] == photo_card_id:
            card["images"].append(s3_key)
            break
    _save(data)


def list_map_points() -> list[dict]:
    """One entry per callsign that has at least one photo card and a
    known lat/lon -- what the public map plots as pins."""
    data = _load()
    counts: dict[str, int] = {}
    for card in data["photo_cards"]:
        counts[card["callsign"]] = counts.get(card["callsign"], 0) + 1

    points = []
    for callsign, loc in data["callsign_locations"].items():
        if loc.get("lat") is None or loc.get("lon") is None:
            continue
        card_count = counts.get(callsign, 0)
        if not card_count:
            continue
        points.append({
            "callsign": callsign,
            "lat": loc["lat"],
            "lon": loc["lon"],
            "country": loc.get("country"),
            "state": loc.get("state"),
            "card_count": card_count,
        })
    return points


def get_cards_for_callsign(callsign: str) -> list[dict]:
    data = _load()
    callsign = callsign.upper()
    cards = [c for c in data["photo_cards"] if c["callsign"] == callsign]
    cards.sort(key=lambda c: (c.get("qso_date") or "", c["created_at"]), reverse=True)
    return cards


def get_images_for_card(photo_card_id: int) -> list[dict]:
    data = _load()
    for card in data["photo_cards"]:
        if card["id"] == photo_card_id:
            return [{"s3_key": key} for key in card["images"]]
    return []


def list_recent_photo_cards(limit: int = 10) -> list[dict]:
    data = _load()
    cards = sorted(data["photo_cards"], key=lambda c: c["created_at"], reverse=True)
    return cards[:limit]


# ---------------------------------------------------------------------
# Josh's own logged QSOs (for auto-fill), imported from ADIF
# ---------------------------------------------------------------------

def import_my_qsos(qsos: list) -> int:
    """Bulk-add parsed ADIF QSOs (adif.AdifQso), skipping ones with no
    callsign. De-duplicates against (callsign, qso_date, band, mode,
    freq) so re-uploading the same or an overlapping log is safe and
    won't create duplicates. Returns how many new entries were added."""
    data = _load()
    existing_keys = {
        (q["callsign"], q.get("qso_date"), q.get("band"), q.get("mode"), q.get("freq"))
        for q in data["my_qsos"]
    }

    added = 0
    for qso in qsos:
        callsign = qso.callsign
        if not callsign:
            continue
        f = qso.fields
        key = (
            callsign,
            f.get("qso_date", ""),
            f.get("band", "").upper(),
            f.get("mode", "").upper(),
            f.get("freq", ""),
        )
        if key in existing_keys:
            continue
        existing_keys.add(key)

        qso_id = data["next_my_qso_id"]
        data["next_my_qso_id"] = qso_id + 1
        data["my_qsos"].append({
            "id": qso_id,
            "callsign": callsign,
            "qso_date": f.get("qso_date", ""),
            "band": f.get("band", "").upper(),
            "mode": f.get("mode", "").upper(),
            "freq": f.get("freq", ""),
            "rst_sent": f.get("rst_sent", ""),
            "rst_rcvd": f.get("rst_rcvd", ""),
            "created_at": time.time(),
        })
        added += 1

    if added:
        _save(data)
    return added


def find_my_qsos(callsign: str) -> list[dict]:
    data = _load()
    callsign = callsign.upper()
    rows = [q for q in data["my_qsos"] if q["callsign"] == callsign]
    rows.sort(key=lambda q: q.get("qso_date") or "", reverse=True)
    return rows


def count_my_qsos() -> int:
    data = _load()
    return len(data["my_qsos"])
