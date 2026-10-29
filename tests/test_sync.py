"""QRZ Logbook sync: duplicates, resume, future dates, ADIF lengths, S3 races.

The fake logbook and in-memory bucket never call QRZ or AWS.
"""
from __future__ import annotations

import unittest
from datetime import date

from adif import AdifQso, parse_adif

import photomap_store
from .support import app, appmod, client_with_token, s3, MemoryBucket


def adif_record(rec: dict) -> str:
    parts = []

    def add(name, value):
        value = "" if value is None else str(value)
        parts.append(f"<{name}:{len(value)}>{value}")

    add("call", rec["call"])
    add("qso_date", rec.get("qso_date", ""))
    add("time_on", rec.get("time_on", "1200"))
    add("band", rec.get("band", "20M"))
    add("mode", rec.get("mode", "FT8"))
    add("freq", rec.get("freq", "14.074"))
    add("app_qrzlog_logid", rec["logid"])
    parts.append("<eor>")
    return "".join(parts)


def qso(call, logid, qso_date="20260115", time_on="1200", band="20M", mode="FT8", freq="14.074"):
    return AdifQso(fields={
        "call": call,
        "qso_date": qso_date,
        "time_on": time_on,
        "band": band,
        "mode": mode,
        "freq": freq,
        "app_qrzlog_logid": str(logid),
    })


class FakeLogbook:
    def __init__(self, records):
        self.records = sorted(records, key=lambda r: r["logid"])
        self.calls = []

    def fetch_log_page(self, api_key, after_logid, page_size=250, timeout=12, since=None, until=None):
        self.calls.append({"after": after_logid, "since": since, "until": until, "timeout": timeout})
        rows = [r for r in self.records if r["logid"] >= int(after_logid)]
        if since:
            floor = since.replace("-", "")
            ceil = (until or since).replace("-", "")
            rows = [r for r in rows if floor <= (r.get("qso_date") or "") <= ceil]
        rows = rows[:page_size]
        return "".join(adif_record(r) for r in rows)

    def fetch_status(self, api_key, timeout=10):
        self.calls.append({"status": True, "timeout": timeout})
        return {"COUNT": str(self.status_count if hasattr(self, "status_count") else len(self.records))}

    def fetch_all_logids(self, api_key, timeout=20):
        return [r["logid"] for r in self.records]

    def fetch_by_logids(self, api_key, logids, timeout=20, deadline=None):
        wanted = {int(i) for i in logids}
        rows = [r for r in self.records if r["logid"] in wanted]
        return "".join(adif_record(r) for r in rows), len(rows)

    def install(self):
        appmod.fetch_log_page = self.fetch_log_page
        appmod.fetch_status = self.fetch_status
        appmod.fetch_all_logids = self.fetch_all_logids
        appmod.fetch_by_logids = self.fetch_by_logids


class AdifLengthTests(unittest.TestCase):
    def test_off_by_one_length_does_not_drop_the_next_field(self):
        text = "<name:5>JOE<qso_date:8>20240101<call:4>K4AA<app_qrzlog_logid:1>7<eor>"
        fields = parse_adif(text)[0].fields
        self.assertEqual(fields["name"], "JOE")
        self.assertEqual(fields["call"], "K4AA")
        self.assertEqual(fields["qso_date"], "20240101")
        self.assertEqual(fields["app_qrzlog_logid"], "7")

    def test_utf8_byte_length_still_parses(self):
        text = "<name:5>José<call:4>K4AA<qso_date:8>20240102<eor>"
        fields = parse_adif(text)[0].fields
        self.assertEqual(fields["name"], "José")
        self.assertEqual(fields["call"], "K4AA")
        self.assertEqual(fields["qso_date"], "20240102")

    def test_long_length_does_not_swallow_the_next_record(self):
        text = (
            "<call:4>K4AA<name:80>JOE<qso_date:8>20240101<eor>"
            "<call:4>W1AW<qso_date:8>20240102<eor>"
        )
        qsos = parse_adif(text)
        self.assertEqual(len(qsos), 2)
        self.assertEqual(qsos[0].fields["qso_date"], "20240101")
        self.assertEqual(qsos[1].callsign, "W1AW")
        self.assertEqual(qsos[1].fields["qso_date"], "20240102")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.bucket = MemoryBucket()
        self.bucket.install()
        self.bucket.reset()
        self._budget = appmod.QRZ_SYNC_TIME_BUDGET_SECONDS
        self._hard = appmod.QRZ_SYNC_HARD_LIMIT_SECONDS
        self._fetch = appmod.fetch_log_page
        self._status = appmod.fetch_status
        self._ids = appmod.fetch_all_logids
        self._byids = appmod.fetch_by_logids

    def tearDown(self):
        appmod.QRZ_SYNC_TIME_BUDGET_SECONDS = self._budget
        appmod.QRZ_SYNC_HARD_LIMIT_SECONDS = self._hard
        appmod.fetch_log_page = self._fetch
        appmod.fetch_status = self._status
        appmod.fetch_all_logids = self._ids
        appmod.fetch_by_logids = self._byids
        self.bucket.uninstall()

    def _admin(self):
        return client_with_token(is_admin=True)

    def _sync(self, client=None):
        client = client or self._admin()
        return client.post(
            "/admin/log/sync",
            data={"csrf_token": "tok"},
            follow_redirects=True,
        )

    def test_find_missing_records_qrz_duplicates(self):
        records = []
        for i in range(5):
            records.append({"logid": 2 * i + 1, "call": f"D{i}", "qso_date": "20260115", "time_on": "1200"})
            records.append({"logid": 2 * i + 2, "call": f"D{i}", "qso_date": "20260115", "time_on": "1201"})
        for rec in records:
            if rec["logid"] % 2 == 1:
                photomap_store.import_my_qsos([qso(rec["call"], rec["logid"], time_on=rec["time_on"])])
        photomap_store.save_qrz_sync_state({
            "caught_up": True, "next_logid": 100, "mode": None, "qrz_dupe_logids": [],
        })
        book = FakeLogbook(records)
        book.install()
        client = self._admin()

        report = client.post("/admin/log/missing", data={"csrf_token": "tok"}, follow_redirects=True)
        self.assertIn(b"Mark 5 as QRZ duplicates", report.data)
        self.assertIn(b"Treated as the same contact", report.data)
        self.assertNotIn(b"Import the", report.data)

        done = client.post(
            "/admin/log/missing",
            data={"csrf_token": "tok", "import": "1"},
            follow_redirects=True,
        )
        self.assertIn(b"Marked 5 as QRZ duplicates", done.data)
        stored = photomap_store.stored_qrz_logids()
        for logid in range(1, 11):
            self.assertIn(str(logid), stored)
        self.assertEqual(photomap_store.count_my_qsos(), 5)

        # A second Find missing no longer lists them.
        again = client.post("/admin/log/missing", data={"csrf_token": "tok"}, follow_redirects=True)
        self.assertIn(b"0", again.data)
        self.assertNotIn(b"Mark 5 as QRZ duplicates", again.data)

    def test_partial_incremental_resumes_without_a_date_window(self):
        today = date.today().strftime("%Y%m%d")
        for i in range(5):
            photomap_store.import_my_qsos([qso(f"S{i}", i + 1, qso_date=today)])
        photomap_store.save_qrz_sync_state({
            "caught_up": True, "next_logid": 6, "mode": None, "qrz_dupe_logids": [],
        })
        records = []
        logid = 6
        for i in range(260):
            records.append({"logid": logid, "call": f"R{i:04d}", "qso_date": today})
            logid += 1
        for i in range(30):
            records.append({"logid": logid, "call": f"OLD{i}", "qso_date": "19990101"})
            logid += 1
        for i in range(100):
            records.append({"logid": logid, "call": f"N{i:04d}", "qso_date": today})
            logid += 1
        book = FakeLogbook(records)
        book.install()

        appmod.QRZ_SYNC_TIME_BUDGET_SECONDS = 0
        first = self._sync()
        self.assertIn(b"Stopped partway", first.data)
        state = photomap_store.qrz_sync_state()
        self.assertEqual(state.get("mode"), "incremental")
        self.assertFalse(state.get("caught_up"))
        self.assertEqual(photomap_store.count_my_qsos(), 5 + 250)
        resume_from = state["next_logid"]

        appmod.QRZ_SYNC_TIME_BUDGET_SECONDS = self._budget
        second = self._sync()
        self.assertIn(b"Up to date", second.data)
        calls = {row["callsign"] for row in photomap_store.list_all_my_qsos()}
        for i in range(30):
            self.assertIn(f"OLD{i}", calls)
        # The resume itself must not have been a date-window fetch.
        resumed = [c for c in book.calls if c.get("after") == resume_from]
        self.assertTrue(resumed, book.calls)
        self.assertIsNone(resumed[0]["since"])

    def test_future_date_does_not_freeze_the_window(self):
        photomap_store.import_my_qsos([qso("FUTURE", 1, qso_date="20990101")])
        self.assertEqual(photomap_store.newest_qso_date(), "")
        photomap_store.save_qrz_sync_state({
            "since": "2099-01-01", "caught_up": False, "next_logid": 0, "mode": "window",
        })
        today = date.today().strftime("%Y%m%d")
        book = FakeLogbook([{"logid": 9, "call": "W1AW", "qso_date": today}])
        book.install()
        response = self._sync()
        self.assertIn(b"Up to date", response.data)
        calls = {row["callsign"] for row in photomap_store.list_all_my_qsos()}
        self.assertIn("W1AW", calls)
        self.assertNotEqual(photomap_store.qrz_sync_state().get("since"), "2099-01-01")
        self.assertTrue(book.calls[0]["since"] is None)

    def test_blank_date_is_backfilled_for_the_same_logid(self):
        photomap_store.import_my_qsos([qso("K4AA", 7, qso_date="")])
        photomap_store.import_my_qsos([qso("K4AA", 7, qso_date="20240101")])
        rows = photomap_store.list_all_my_qsos()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qso_date"], "20240101")
        self.assertEqual(rows[0]["qrz_logid"], "7")

    def test_unparsed_page_does_not_mark_caught_up(self):
        book = FakeLogbook([])
        book.fetch_log_page = lambda *a, **k: "<eor>"
        book.install()
        response = self._sync()
        self.assertIn(b"stopped instead of skipping", response.data)
        self.assertFalse(photomap_store.qrz_sync_state().get("caught_up"))

    def test_hard_limit_refuses_to_start_a_call(self):
        calls = []

        def fetch(*_a, **_k):
            calls.append(True)
            return ""

        appmod.fetch_log_page = fetch
        appmod.QRZ_SYNC_HARD_LIMIT_SECONDS = -1
        response = self._sync()
        self.assertEqual(calls, [])
        self.assertIn(b"Stopped partway", response.data)

    def test_conflict_retries_instead_of_dropping_qsos(self):
        photomap_store.import_my_qsos([qso("AAAA", 1)])
        data, etag = s3.get_json_meta(photomap_store.INDEX_KEY)
        self.bucket.conflicts_remaining = 1
        photomap_store.import_my_qsos([qso("BBBB", 2)])
        calls = {row["callsign"] for row in photomap_store.list_all_my_qsos()}
        self.assertEqual(calls, {"AAAA", "BBBB"})
        with self.assertRaises(s3.S3Conflict):
            s3.put_json(photomap_store.INDEX_KEY, data, if_match=etag)
        calls = {row["callsign"] for row in photomap_store.list_all_my_qsos()}
        self.assertIn("BBBB", calls)

    def test_status_gap_points_at_find_missing(self):
        today = date.today().strftime("%Y%m%d")
        book = FakeLogbook([{"logid": 1, "call": "W1AW", "qso_date": today}])
        book.status_count = 40
        book.install()
        response = self._sync()
        self.assertIn(b"STATUS reports 40", response.data)
        self.assertIn(b"Find missing QSOs", response.data)
        self.assertNotIn(b"pull in the rest", response.data)


if __name__ == "__main__":
    unittest.main()
