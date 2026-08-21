"""
SQLite storage for looked-up contacts and QRZ auth state.

Every row is tagged with an anonymous `session_id` (a random token kept
in the visitor's Flask session cookie) so concurrent visitors never see
each other's results -- there are no user accounts. Rows older than
SESSION_TTL_HOURS are purged on access so nothing lingers indefinitely.

The QRZ session key (obtained once at login, never the password itself)
lives here too, in `auth_sessions`, rather than in the cookie -- the
cookie only ever holds the anonymous `session_id` used to look it up.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent / "instance" / "qsl_tracker.db"
SESSION_TTL_HOURS = 24

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    callsign TEXT NOT NULL,
    name TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    zip_code TEXT,
    country TEXT,
    grid TEXT,
    mqsl INTEGER NOT NULL DEFAULT 0,
    eqsl INTEGER NOT NULL DEFAULT 0,
    lotw INTEGER NOT NULL DEFAULT 0,
    qsl_via TEXT,
    accepts_direct INTEGER NOT NULL DEFAULT 0,
    looked_up_at REAL NOT NULL,
    UNIQUE(session_id, callsign)
);
CREATE INDEX IF NOT EXISTS idx_contacts_session ON contacts(session_id);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id TEXT PRIMARY KEY,
    qrz_key TEXT NOT NULL,
    qrz_username TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS qsl_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    callsign TEXT NOT NULL,
    note TEXT,
    contact_email TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_qsl_requests_session ON qsl_requests(session_id);

-- Everything below is for the QSL Photo Map (admin-only upload, public
-- map view). Deliberately separate from `contacts` above -- that table
-- is ephemeral per-visitor scratch data purged after 24h; this is
-- Josh's own durable collection of scanned cards and is never purged.

-- One row per callsign, cached from QRZ's XML API so re-uploading a
-- card for the same station doesn't re-hit QRZ every time. Distinct
-- from lookup_callsign()'s mailing-address fields in qrz.py -- this is
-- just an approximate public location for a map pin (country/state/grid
-- /lat/lon), never a street address.
CREATE TABLE IF NOT EXISTS callsign_locations (
    callsign TEXT PRIMARY KEY,
    country TEXT,
    state TEXT,
    county TEXT,
    grid TEXT,
    lat REAL,
    lon REAL,
    looked_up_at REAL NOT NULL
);

-- One row per upload ("this card, from this QSO"). A callsign can have
-- several of these (repeat contacts, multiple cards) -- the map groups
-- them by callsign via callsign_locations and shows them all in one
-- station's popup.
CREATE TABLE IF NOT EXISTS photo_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    qso_date TEXT,
    band TEXT,
    mode TEXT,
    freq TEXT,
    rst_sent TEXT,
    rst_rcvd TEXT,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_cards_callsign ON photo_cards(callsign);

-- One row per uploaded image (front-of-card only, but a card entry can
-- have more than one photo). s3_key points into the private S3 bucket;
-- see s3.py -- the app always hands out short-lived presigned GET URLs
-- rather than making the bucket public.
CREATE TABLE IF NOT EXISTS photo_card_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_card_id INTEGER NOT NULL,
    s3_key TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_card_images_card ON photo_card_images(photo_card_id);

-- Josh's own confirmed QSOs, imported from his ADIF log (admin-only,
-- see /admin/photomap/import-adif) so the photo-upload form can offer
-- to auto-fill date/band/mode/freq/RST for a callsign instead of typing
-- it by hand every time. Durable, not purged -- distinct from the
-- per-visitor ADIF upload on the main dashboard, which only drives QRZ
-- lookups and doesn't keep QSO details at all.
CREATE TABLE IF NOT EXISTS my_qsos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    callsign TEXT NOT NULL,
    qso_date TEXT,
    band TEXT,
    mode TEXT,
    freq TEXT,
    rst_sent TEXT,
    rst_rcvd TEXT,
    created_at REAL NOT NULL,
    UNIQUE(callsign, qso_date, band, mode, freq)
);
CREATE INDEX IF NOT EXISTS idx_my_qsos_callsign ON my_qsos(callsign);
"""


@contextmanager
def get_conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def purge_expired() -> None:
    # qsl_requests is deliberately NOT purged here -- unlike contacts and
    # auth_sessions (which are ephemeral per-visitor scratch data), it's
    # a small log of incoming card requests that's useful to keep around
    # as a backup to the email notification. (It's still wiped whenever
    # Render redeploys/restarts, since the free plan's disk isn't
    # persistent -- the email is the durable record.)
    cutoff = time.time() - SESSION_TTL_HOURS * 3600
    with get_conn() as conn:
        conn.execute("DELETE FROM contacts WHERE looked_up_at < ?", (cutoff,))
        conn.execute("DELETE FROM auth_sessions WHERE created_at < ?", (cutoff,))


def save_auth(session_id: str, qrz_key: str, qrz_username: str) -> None:
    """Store (or replace) the QRZ session key for an anonymous session."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_sessions (session_id, qrz_key, qrz_username, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                qrz_key=excluded.qrz_key,
                qrz_username=excluded.qrz_username,
                created_at=excluded.created_at
            """,
            (session_id, qrz_key, qrz_username, time.time()),
        )


def get_auth(session_id: str) -> sqlite3.Row | None:
    """Return the (qrz_key, qrz_username) row for a session, or None."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT qrz_key, qrz_username FROM auth_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()


def clear_auth(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM auth_sessions WHERE session_id = ?", (session_id,))


def upsert_contact(session_id: str, record) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO contacts (
                session_id, callsign, name, address, city, state, zip_code,
                country, grid, mqsl, eqsl, lotw, qsl_via, accepts_direct,
                looked_up_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, callsign) DO UPDATE SET
                name=excluded.name,
                address=excluded.address,
                city=excluded.city,
                state=excluded.state,
                zip_code=excluded.zip_code,
                country=excluded.country,
                grid=excluded.grid,
                mqsl=excluded.mqsl,
                eqsl=excluded.eqsl,
                lotw=excluded.lotw,
                qsl_via=excluded.qsl_via,
                accepts_direct=excluded.accepts_direct,
                looked_up_at=excluded.looked_up_at
            """,
            (
                session_id,
                record.callsign,
                record.name,
                record.address,
                record.city,
                record.state,
                record.zip_code,
                record.country,
                record.grid,
                int(record.mqsl),
                int(record.eqsl),
                int(record.lotw),
                record.qsl_via,
                int(record.accepts_direct),
                time.time(),
            ),
        )


def get_contacts(session_id: str, direct_only: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM contacts WHERE session_id = ?"
    params: list = [session_id]
    if direct_only:
        query += " AND accepts_direct = 1"
    query += " ORDER BY looked_up_at DESC"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def clear_session(session_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM contacts WHERE session_id = ?", (session_id,))


def save_qsl_request(session_id: str, callsign: str, note: str, contact_email: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO qsl_requests (session_id, callsign, note, contact_email, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, callsign, note, contact_email, time.time()),
        )


def count_recent_qsl_requests(session_id: str, window_seconds: int) -> int:
    """How many QSL card requests this anonymous session has made in the
    last `window_seconds` -- used to rate-limit the public request form."""
    cutoff = time.time() - window_seconds
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM qsl_requests WHERE session_id = ? AND created_at > ?",
            (session_id, cutoff),
        ).fetchone()
        return row["n"] if row else 0


# ---------------------------------------------------------------------
# QSL Photo Map
# ---------------------------------------------------------------------

def get_callsign_location(callsign: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM callsign_locations WHERE callsign = ?", (callsign.upper(),)
        ).fetchone()


def save_callsign_location(loc) -> None:
    """`loc` is a qrz.QrzLocation. Cached indefinitely -- re-fetch by
    deleting the row if a station's QRZ info ever needs refreshing."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO callsign_locations
                (callsign, country, state, county, grid, lat, lon, looked_up_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(callsign) DO UPDATE SET
                country=excluded.country,
                state=excluded.state,
                county=excluded.county,
                grid=excluded.grid,
                lat=excluded.lat,
                lon=excluded.lon,
                looked_up_at=excluded.looked_up_at
            """,
            (
                loc.callsign.upper(),
                loc.country,
                loc.state,
                loc.county,
                loc.grid,
                loc.lat,
                loc.lon,
                time.time(),
            ),
        )


def add_photo_card(
    callsign: str, qso_date: str, band: str, mode: str, freq: str,
    rst_sent: str, rst_rcvd: str, note: str,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO photo_cards
                (callsign, qso_date, band, mode, freq, rst_sent, rst_rcvd, note, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (callsign.upper(), qso_date, band, mode, freq, rst_sent, rst_rcvd, note, time.time()),
        )
        return cur.lastrowid


def add_photo_card_image(photo_card_id: int, s3_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO photo_card_images (photo_card_id, s3_key, created_at) VALUES (?, ?, ?)",
            (photo_card_id, s3_key, time.time()),
        )


def list_map_points() -> list[sqlite3.Row]:
    """One row per callsign that has at least one photo card and a known
    lat/lon -- what the public map plots as pins."""
    with get_conn() as conn:
        return conn.execute(
            """
            SELECT cl.callsign, cl.lat, cl.lon, cl.country, cl.state,
                   COUNT(pc.id) AS card_count
            FROM callsign_locations cl
            JOIN photo_cards pc ON pc.callsign = cl.callsign
            WHERE cl.lat IS NOT NULL AND cl.lon IS NOT NULL
            GROUP BY cl.callsign
            """
        ).fetchall()


def get_cards_for_callsign(callsign: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM photo_cards WHERE callsign = ? ORDER BY qso_date DESC, created_at DESC",
            (callsign.upper(),),
        ).fetchall()


def get_images_for_card(photo_card_id: int) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM photo_card_images WHERE photo_card_id = ? ORDER BY id",
            (photo_card_id,),
        ).fetchall()


def list_recent_photo_cards(limit: int = 10) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM photo_cards ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()


def import_my_qsos(qsos: list) -> int:
    """Bulk-insert parsed ADIF QSOs (adif.AdifQso) into my_qsos, skipping
    ones with no callsign. INSERT OR IGNORE against the UNIQUE constraint
    means re-uploading the same log (or an overlapping one) is safe and
    won't create duplicates. Returns how many new rows were added."""
    added = 0
    with get_conn() as conn:
        for qso in qsos:
            callsign = qso.callsign
            if not callsign:
                continue
            f = qso.fields
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO my_qsos
                    (callsign, qso_date, band, mode, freq, rst_sent, rst_rcvd, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    callsign,
                    f.get("qso_date", ""),
                    f.get("band", "").upper(),
                    f.get("mode", "").upper(),
                    f.get("freq", ""),
                    f.get("rst_sent", ""),
                    f.get("rst_rcvd", ""),
                    time.time(),
                ),
            )
            if cur.rowcount:
                added += 1
    return added


def find_my_qsos(callsign: str) -> list[sqlite3.Row]:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM my_qsos WHERE callsign = ? ORDER BY qso_date DESC",
            (callsign.upper(),),
        ).fetchall()


def count_my_qsos() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM my_qsos").fetchone()
        return row["n"] if row else 0
