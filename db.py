"""
SQLite storage for looked-up contacts.

Every row is tagged with an anonymous `session_id` (a random token kept
in the visitor's Flask session cookie) so concurrent visitors never see
each other's results -- there are no user accounts. Rows older than
SESSION_TTL_HOURS are purged on access so nothing lingers indefinitely.
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
    cutoff = time.time() - SESSION_TTL_HOURS * 3600
    with get_conn() as conn:
        conn.execute("DELETE FROM contacts WHERE looked_up_at < ?", (cutoff,))


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
