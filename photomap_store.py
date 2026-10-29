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
compared to a real database -- no partial updates -- but this is a
low-traffic, single-admin tool. Overlapping writes don't clobber each
other: the save is conditional on the object's ETag (S3 If-Match), and
a conflict re-reads and re-applies the change.
"""
from __future__ import annotations

import time
from datetime import date

import dxcc
import s3
from s3 import S3Error

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
        # QRZ Logbook sync cursor -- see qrz_sync_state() below.
        "qrz_sync": {},
        # Print-on-the-card alignment -- see card_print_settings() below.
        "card_print": {},
    }


# How many times to re-read and retry a save that lost an If-Match race.
_SAVE_ATTEMPTS = 5


def _load_versioned() -> tuple[dict, str | None]:
    """The store plus the ETag it was read at. ETag is None if the
    object does not exist yet."""
    data, etag = s3.get_json_meta(INDEX_KEY)
    if not isinstance(data, dict):
        return _empty(), None
    # Tolerate an older/partial blob gaining new keys over time.
    base = _empty()
    base.update(data)
    return base, etag


def _load() -> dict:
    data, _etag = _load_versioned()
    return data


def _save(data: dict, etag: str | None) -> None:
    if etag is None:
        # Create only if nobody else created it between our read and write.
        s3.put_json(INDEX_KEY, data, if_none_match="*")
    else:
        s3.put_json(INDEX_KEY, data, if_match=etag)


def _update(mutator):
    """Run mutator(data) -> (should_save, result) and write if needed.

    mutator has to be safe to run again on a fresh copy: a conflicting
    save throws the in-memory copy away and starts over. The result from
    the attempt that finished (saved, or nothing to save) is what comes
    back."""
    last = None
    for _attempt in range(_SAVE_ATTEMPTS):
        data, etag = _load_versioned()
        should_save, result = mutator(data)
        if not should_save:
            return result
        try:
            _save(data, etag)
            return result
        except s3.S3Conflict as exc:
            last = exc
    raise S3Error(
        "The photo map data changed while it was being saved. Try again."
    ) from last


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
    def mutate(data):
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
        return True, None
    _update(mutate)


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
    def mutate(data):
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
        return True, card_id
    return _update(mutate)


def add_photo_card_image(photo_card_id: int, s3_key: str) -> None:
    def mutate(data):
        for card in data["photo_cards"]:
            if card["id"] == photo_card_id:
                card["images"].append(s3_key)
                return True, None
        return False, None
    _update(mutate)


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


def list_all_photo_cards() -> list[dict]:
    """Every photo card, callsign then most-recent-first -- the admin
    "manage cards" page's full list (list_recent_photo_cards above only
    shows a handful, for the upload page's quick-glance table)."""
    data = _load()
    return sorted(data["photo_cards"], key=lambda c: (c["callsign"], -c["created_at"]))


def get_photo_card(card_id: int) -> dict | None:
    data = _load()
    for card in data["photo_cards"]:
        if card["id"] == card_id:
            return card
    return None


def update_photo_card(
    card_id: int, qso_date: str, band: str, mode: str, freq: str,
    rst_sent: str, rst_rcvd: str, note: str,
) -> bool:
    """Overwrites a card's QSO fields in place -- for fixing entries
    that were uploaded without QSO info (or with the wrong info).
    Returns False if no such card exists (nothing to do)."""
    def mutate(data):
        for card in data["photo_cards"]:
            if card["id"] == card_id:
                card["qso_date"] = qso_date
                card["band"] = band
                card["mode"] = mode
                card["freq"] = freq
                card["rst_sent"] = rst_sent
                card["rst_rcvd"] = rst_rcvd
                card["note"] = note
                return True, True
        return False, False
    return _update(mutate)


def remove_photo_card_image(card_id: int, s3_key: str) -> bool:
    """Detaches one image from a card (e.g. a bad scan). Doesn't touch
    the S3 object itself -- see s3.delete_object(), called separately
    by the route so a failed S3 delete doesn't block the metadata
    update. Returns False if the card or that image reference wasn't
    found."""
    def mutate(data):
        for card in data["photo_cards"]:
            if card["id"] == card_id and s3_key in card["images"]:
                card["images"].remove(s3_key)
                return True, True
        return False, False
    return _update(mutate)


def delete_photo_card(card_id: int) -> dict | None:
    """Removes a card entirely -- for entries that were uploaded
    without a photo or QSO info and are easier to redo than fix.
    Returns the removed card (so the caller can clean up its S3 image
    objects) or None if no such card exists."""
    def mutate(data):
        for i, card in enumerate(data["photo_cards"]):
            if card["id"] == card_id:
                removed = data["photo_cards"].pop(i)
                return True, removed
        return False, None
    return _update(mutate)


# ---------------------------------------------------------------------
# Josh's own logged QSOs (for auto-fill), imported from ADIF
# ---------------------------------------------------------------------

def _entity_for(callsign: str, adif_country: str) -> str:
    """DXCC entity for one imported QSO -- prefer whatever Josh's own
    logging software already wrote into the ADIF's COUNTRY field (it
    presumably knows more than a static prefix table, e.g. about a
    specific special-event call), and only fall back to deriving it
    from the callsign prefix (see dxcc.py -- best-effort, not
    authoritative) when the log didn't say. Either way this can come
    back empty, same as any other optional field here."""
    if adif_country:
        return adif_country
    return dxcc.entity_for_callsign(callsign) or ""


def _norm_freq(freq) -> str:
    """Frequency as a dedupe-key string that doesn't care how a logger
    formatted it -- "14.074", "14.07400" and "14.0740" all become
    "14.074". Without this, the same QSO arriving once from an ADIF
    upload and once from a QRZ Logbook sync (two different programs
    writing the same number differently) would be stored twice."""
    try:
        return f"{float(freq):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return (freq or "").strip()


def _dedupe_key(callsign, qso_date, band, mode, freq) -> tuple:
    return (callsign, qso_date or "", (band or "").upper(), (mode or "").upper(), _norm_freq(freq))


def _minutes(time_on: str) -> int | None:
    """ADIF time_on ("HHMM" or "HHMMSS") -> minutes after midnight UTC."""
    t = (time_on or "").strip()
    if len(t) < 4 or not t[:4].isdigit():
        return None
    return int(t[:2]) * 60 + int(t[2:4])


def _find_same_qso(candidates: list[dict], time_on: str) -> dict | None:
    """The stored row (among those sharing callsign/date/band/mode/freq)
    that is the same contact as an incoming one with `time_on`. Times
    within 2 minutes count as the same contact (a logger and QRZ can
    round the start time differently); if either side has no time at
    all, fall back to the old time-blind match so older imports without
    times still dedupe instead of doubling."""
    incoming = _minutes(time_on)
    untimed = None
    for row in candidates:
        stored = _minutes(row.get("time_on", ""))
        if incoming is None or stored is None:
            untimed = untimed or row
            continue
        if abs(incoming - stored) <= 2 or abs(incoming - stored) >= 1438:  # wraps midnight
            return row
    return untimed


def _row_key(row: dict) -> tuple:
    return _dedupe_key(row["callsign"], row.get("qso_date"), row.get("band"), row.get("mode"), row.get("freq"))


def _move_row(existing: dict, row: dict, old_key: tuple, new_key: tuple) -> None:
    bucket = existing.get(old_key)
    if bucket and row in bucket:
        bucket.remove(row)
        if not bucket:
            existing.pop(old_key, None)
    existing.setdefault(new_key, [])
    if row not in existing[new_key]:
        existing[new_key].append(row)


def _fill_blanks(row: dict, values: dict) -> bool:
    """Fill only fields that are empty on `row`. Never overwrites."""
    changed = False
    for key, value in values.items():
        if value and not row.get(key):
            row[key] = value
            changed = True
    return changed


def _import_into(data: dict, qsos: list, qrz_dupes: set) -> tuple[int, int]:
    """Mutate `data` in place. `qrz_dupes` collects second QRZ log ids."""
    # Several real QSOs can share (callsign, date, band, mode, freq) --
    # FT8 on one dial frequency, a POTA re-contact, a QSO-party rover --
    # so each key holds a list, and time_on decides which (if any) is the
    # same contact. See _find_same_qso(). A QRZ log id is an even stronger
    # match: the same id is the same QRZ record even when an earlier
    # import stored it with a blank date.
    existing: dict[tuple, list[dict]] = {}
    by_logid: dict[str, dict] = {}
    for q in data["my_qsos"]:
        existing.setdefault(_row_key(q), []).append(q)
        if q.get("qrz_logid"):
            by_logid[str(q["qrz_logid"])] = q

    added = 0
    backfilled = 0
    for qso in qsos:
        callsign = qso.callsign
        if not callsign:
            continue
        f = qso.fields
        qrz_logid = str(f.get("app_qrzlog_logid") or "")
        time_on = f.get("time_on", "")
        band = (f.get("band") or "").upper()
        mode = (f.get("mode") or "").upper()
        freq = f.get("freq", "")
        qso_date = f.get("qso_date", "")
        gridsquare = f.get("gridsquare", "")
        dxcc_entity = _entity_for(callsign, f.get("country", ""))
        blanks = {
            "qso_date": qso_date,
            "time_on": time_on,
            "band": band,
            "mode": mode,
            "freq": freq,
            "rst_sent": f.get("rst_sent", ""),
            "rst_rcvd": f.get("rst_rcvd", ""),
            "gridsquare": gridsquare,
            "dxcc_entity": dxcc_entity,
        }

        if qrz_logid and qrz_logid in by_logid:
            row = by_logid[qrz_logid]
            old_key = _row_key(row)
            if _fill_blanks(row, blanks):
                backfilled += 1
                new_key = _row_key(row)
                if new_key != old_key:
                    _move_row(existing, row, old_key, new_key)
            continue

        key = _dedupe_key(callsign, qso_date, band, mode, freq)
        existing_row = _find_same_qso(existing.get(key, []), time_on)
        if existing_row is not None:
            old_key = _row_key(existing_row)
            row_changed = _fill_blanks(existing_row, blanks)
            if qrz_logid and not existing_row.get("qrz_logid"):
                existing_row["qrz_logid"] = qrz_logid
                by_logid[qrz_logid] = existing_row
                row_changed = True
            elif qrz_logid and existing_row.get("qrz_logid") not in ("", None, qrz_logid):
                # A *second* QRZ entry for a contact we already hold under a
                # different QRZ log id: QRZ has the same QSO twice (e.g.
                # uploaded by both the logger and WSJT-X). Counted once here.
                qrz_dupes.add(qrz_logid)
            if row_changed:
                backfilled += 1
                new_key = _row_key(existing_row)
                if new_key != old_key:
                    _move_row(existing, existing_row, old_key, new_key)
            continue

        qso_id = data["next_my_qso_id"]
        data["next_my_qso_id"] = qso_id + 1
        new_row = {
            "id": qso_id,
            "callsign": callsign,
            "qso_date": qso_date,
            "time_on": time_on,
            "band": band,
            "mode": mode,
            "freq": freq,
            "rst_sent": blanks["rst_sent"],
            "rst_rcvd": blanks["rst_rcvd"],
            "gridsquare": gridsquare,
            "dxcc_entity": dxcc_entity,
            "qrz_logid": qrz_logid,
            "created_at": time.time(),
        }
        data["my_qsos"].append(new_row)
        existing.setdefault(key, []).append(new_row)
        if qrz_logid:
            by_logid[qrz_logid] = new_row
        added += 1

    return added, backfilled


def import_my_qsos(qsos: list, qrz_dupes: set | None = None) -> tuple[int, int]:
    """Bulk-add parsed ADIF QSOs (adif.AdifQso), skipping ones with no
    callsign. De-duplicates against (callsign, qso_date, band, mode,
    freq) plus time_on within 2 minutes (see _find_same_qso()) so re-uploading the same or an overlapping log is safe and
    won't create duplicates. Also backfills empty `qso_date`/`band`/
    `mode`/`freq`/`time_on`/`gridsquare`/`dxcc_entity` on a row that's
    already here -- including when the match is the same QRZ log id but
    the stored date was blank. A second QRZ log id for the same contact
    is added to `qrz_dupes` when that set is passed. Returns (added,
    backfilled) counts."""
    def mutate(data):
        # Local set so a retried save doesn't record dupes from a
        # discarded attempt. The caller sees only the attempt that stuck.
        local: set = set()
        added, backfilled = _import_into(data, qsos, local)
        return bool(added or backfilled), (added, backfilled, local)

    added, backfilled, local = _update(mutate)
    if qrz_dupes is not None:
        qrz_dupes.update(local)
    return added, backfilled


def _newest_first_key(q: dict) -> tuple:
    """Sort key for "newest on top": date, then UTC time within the day
    (HHMM / HHMMSS padded so "0930" < "093015" < "1400" compare right)."""
    return (q.get("qso_date") or "", (q.get("time_on") or "").ljust(6, "0"))


def find_my_qsos(callsign: str) -> list[dict]:
    data = _load()
    callsign = callsign.upper()
    rows = [q for q in data["my_qsos"] if q["callsign"] == callsign]
    rows.sort(key=_newest_first_key, reverse=True)
    return rows


def get_my_qso(qso_id: int) -> dict | None:
    """One logged QSO by id -- used by the QSO-label page once a
    specific contact has been picked from find_my_qsos()'s list."""
    data = _load()
    for q in data["my_qsos"]:
        if q["id"] == qso_id:
            return q
    return None


def count_my_qsos() -> int:
    data = _load()
    return len(data["my_qsos"])


def list_all_my_qsos(dxcc_entity: str = "", callsign: str = "") -> list[dict]:
    """Every logged QSO, most recent first -- the "browse by DXCC
    entity" page's data source. `dxcc_entity` (exact match) and
    `callsign` (substring, so a partial callsign still narrows things
    down) filter when given; either or both blank means no filtering
    on that axis. A QSO with no captured entity (empty string) is
    simply excluded whenever a specific entity is picked -- there's
    nothing to match."""
    data = _load()
    rows = data["my_qsos"]
    if dxcc_entity:
        rows = [q for q in rows if q.get("dxcc_entity") == dxcc_entity]
    if callsign:
        callsign = callsign.upper()
        rows = [q for q in rows if callsign in q["callsign"]]
    return sorted(rows, key=_newest_first_key, reverse=True)


def list_dxcc_entities() -> list[str]:
    """Distinct, non-empty DXCC entity names present across every
    logged QSO, alphabetical -- populates the browse page's filter
    dropdown. Entities only ever come from what's actually been
    captured on import (ADIF COUNTRY field, or the prefix-derived
    fallback -- see dxcc.py), so this naturally reflects however
    complete or incomplete that capture is."""
    data = _load()
    seen = {q["dxcc_entity"] for q in data["my_qsos"] if q.get("dxcc_entity")}
    return sorted(seen)


# ---------------------------------------------------------------------
# QRZ Logbook sync cursor
# ---------------------------------------------------------------------

def qrz_sync_state() -> dict:
    """Where the QRZ Logbook sync left off: `next_logid` (the AFTERLOGID
    to ask for next -- 0 means "start of the logbook"), `last_sync` (unix
    time of the last page fetched), and `caught_up` (True once a sync
    reached the end of the logbook, so the next one only pulls new QSOs).
    Empty dict if a sync has never run."""
    return dict(_load().get("qrz_sync") or {})


def save_qrz_sync_state(state: dict) -> None:
    """Merge `state` into the saved sync state (keys not given are kept --
    e.g. the last-seen QRZ total survives a mid-sync cursor save)."""
    def mutate(data):
        merged = dict(data.get("qrz_sync") or {})
        merged.update(state)
        if merged == (data.get("qrz_sync") or {}):
            return False, None
        data["qrz_sync"] = merged
        return True, None
    _update(mutate)


# ---------------------------------------------------------------------
# Print-on-the-card alignment
# ---------------------------------------------------------------------

def card_print_settings() -> dict:
    """The printer nudge for printing QSO details straight onto the QSL
    card: `nudge_x` / `nudge_y` in inches (+ = right / down). Zeros until
    Josh saves something."""
    saved = _load().get("card_print") or {}
    out = {"nudge_x": 0.0, "nudge_y": 0.0}
    for key in out:
        try:
            out[key] = float(saved.get(key, 0.0))
        except (TypeError, ValueError):
            pass
    return out


def save_card_print_settings(nudge_x: float, nudge_y: float) -> None:
    def mutate(data):
        data["card_print"] = {"nudge_x": nudge_x, "nudge_y": nudge_y}
        return True, None
    _update(mutate)


def newest_qso_date() -> str:
    """Latest `qso_date` (ADIF YYYYMMDD) anywhere in the log, or "" if the
    log is empty -- where the first QRZ sync starts its date window.

    Dates after today are ignored. One imported row dated next year used
    to push the first-sync window into the future, so every later sync
    fetched nothing and stayed there."""
    today = date.today().strftime("%Y%m%d")
    dates = [q.get("qso_date") or "" for q in _load()["my_qsos"]]
    return max((d for d in dates if len(d) == 8 and d.isdigit() and d <= today), default="")


def stored_qrz_logids() -> set[str]:
    """QRZ log ids attached to rows in the log, plus ones recorded as QRZ-
    side duplicates -- i.e. every QRZ entry the app has accounted for."""
    data = _load()
    ids = {str(q["qrz_logid"]) for q in data["my_qsos"] if q.get("qrz_logid")}
    ids |= {str(x) for x in (data.get("qrz_sync") or {}).get("qrz_dupe_logids") or []}
    return ids


def explain_qsos(qsos: list) -> list[dict]:
    """For each parsed QSO, what import_my_qsos() *would* do with it,
    without saving anything: "new" (would be added), "matches" (treated as
    the same contact as an existing row -- included so a wrong merge is
    visible), or "no callsign" (skipped)."""
    data = _load()
    existing: dict[tuple, list[dict]] = {}
    for q in data["my_qsos"]:
        existing.setdefault(
            _dedupe_key(q["callsign"], q.get("qso_date"), q.get("band"), q.get("mode"), q.get("freq")), []
        ).append(q)
    out = []
    for qso in qsos:
        f = qso.fields
        entry = {
            "logid": f.get("app_qrzlog_logid", ""),
            "callsign": qso.callsign,
            "qso_date": f.get("qso_date", ""),
            "time_on": f.get("time_on", ""),
            "band": f.get("band", "").upper(),
            "mode": f.get("mode", "").upper(),
            "freq": f.get("freq", ""),
        }
        if not qso.callsign:
            entry["outcome"], entry["row"] = "no callsign", None
        else:
            key = _dedupe_key(qso.callsign, f.get("qso_date", ""), f.get("band", ""), f.get("mode", ""), f.get("freq", ""))
            row = _find_same_qso(existing.get(key, []), f.get("time_on", ""))
            entry["outcome"], entry["row"] = ("matches", row) if row else ("new", None)
        out.append(entry)
    return out

