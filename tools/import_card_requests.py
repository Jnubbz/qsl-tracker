"""One-time import of card requests that arrived before the site kept
them in the Unraid database. The rows were copied from the
"QSL card request from ..." notification emails in Josh's Gmail
(2026-11-03). Each one keeps the email's timestamp as its request time and is
imported already ticked as Answered (stamped with the time of import).

Run inside the container (the database lives on the /data volume):

    docker exec qsl-tracker python tools/import_card_requests.py

Safe to run more than once. A row is skipped when the database already
has a request from the same callsign within 15 minutes of the email
(the email is sent seconds after the request is saved).

Left out on purpose: KN0BLE (2026-08-23, Josh testing his own form) and
K4DBO (2026-08-21, note "Test").
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402

SESSION_ID = "gmail-import-20261103"
MATCH_WINDOW_SECONDS = 15 * 60

# (email time UTC, callsign, note, contact email, mailing address)
REQUESTS = [
    ("2026-08-25T16:15:38Z", "IZ6MPZ", "", "crjs69@gmail.com", ""),
    ("2026-09-22T10:28:35Z", "DL1LRI", "", "", ""),
    ("2026-09-23T10:30:12Z", "DL1LRI", "DL1LRI 21.09.2026 12:48 FT8 18,100793 -17", "gerawink@aol.com", ""),
    ("2026-10-04T14:55:03Z", "DL9RMH", "", "", ""),
    ("2026-10-10T07:17:16Z", "DL9RMH", "", "", ""),
    ("2026-10-14T17:35:46Z", "DF6NL", "", "df6nl@darc.de", ""),
    ("2026-10-18T13:19:51Z", "IU0IKZ", "worked you on 10m FT8 18/10/2026", "", ""),
    ("2026-10-20T16:20:44Z", "SP3EMA", "", "sp3ema@gmail.com", ""),
    ("2026-10-26T08:58:51Z", "LZ4BU", "TNX QSO 23-Oct-2026 13:26 21 MHz 2xFT8", "ivo.lz4bu@gmail.com", ""),
    ("2026-10-27T16:38:05Z", "DB5PJ",
     "27.10.2026 16:23 UTC 12 m FT 8 +00 -05 TU for QSO All the best for Your and Yours 73 de Herri",
     "DB5PJ@gmx.de", ""),
    ("2026-10-28T15:39:09Z", "DL7AQT", "wkd FT-8 oct 28 2026 1437 utc on 21Mhz - sent -6 receifed -5",
     "dl7aqt@darc.de", ""),
    ("2026-10-30T15:33:41Z", "F8BFU", "", "", ""),
    ("2026-11-01T01:28:36Z", "JA0IAA", "2026/10/20 0038U 12m FT8 SEND-15 RCVD -19", "iaasada73@nifty.com",
     "HIROSHI SADA\n1-2-21, HONJO, MITSUKE, NIIGATA, 954-0051, JAPAN."),
]


def _ts(iso: str) -> float:
    return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def run() -> tuple[int, int]:
    db.init_db()  # adds answered_at on an older database
    added = skipped = 0
    now = time.time()
    with db.get_conn() as conn:
        for iso, callsign, note, email, address in REQUESTS:
            ts = _ts(iso)
            dup = conn.execute(
                "SELECT 1 FROM qsl_requests WHERE callsign = ? AND ABS(created_at - ?) <= ?",
                (callsign, ts, MATCH_WINDOW_SECONDS),
            ).fetchone()
            if dup:
                skipped += 1
                print(f"  skip  {callsign:8} {iso}  (already in the database)")
                continue
            conn.execute(
                """
                INSERT INTO qsl_requests
                    (session_id, callsign, note, contact_email, address, created_at, answered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (SESSION_ID, callsign, note, email, address, ts, now),
            )
            added += 1
            print(f"  added {callsign:8} {iso}")
    return added, skipped


if __name__ == "__main__":
    print(f"Database: {db.DB_PATH}")
    a, s = run()
    print(f"Done: {a} added, {s} already there. All imported requests are ticked Answered.")
