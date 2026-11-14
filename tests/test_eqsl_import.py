"""Automatic eQSL-from-Gmail import endpoint (/admin/photomap/api/import-eqsl).

A scheduled background job (no browser, no admin session) posts here
once it finds a new eQSL attachment in Gmail. Covers: token auth
instead of the admin session/CSRF, saving a card with full QSO info
vs. callsign-only (several real eQSL senders -- QSLMaster's bureau
relay, World Radio League -- never include QSO specifics at all),
QRZ location lookup via the job's own credentials (no human logged
in), and that a QRZ hiccup degrades gracefully instead of 500ing.
"""
from __future__ import annotations

import io
import unittest

import photomap_store
from qrz import QrzError, QrzLocation
from .support import app, appmod, MemoryBucket


def fake_image(filename="card.jpg"):
    return (io.BytesIO(b"\xff\xd8\xff\xe0fakejpegbytes"), filename)


class EqslImportTests(unittest.TestCase):
    def setUp(self):
        self.bucket = MemoryBucket()
        self.bucket.install()
        self.client = app.test_client()

        self.uploaded = []
        self.session_key_calls = []
        self.location_calls = []

        def fake_upload_card_image(file_storage, callsign):
            self.uploaded.append((callsign, file_storage.filename))
            return f"photocards/{callsign.upper()}/fake.jpg"

        def fake_get_session_key(username, password):
            self.session_key_calls.append((username, password))
            return "FAKESESSIONKEY"

        def fake_lookup_location(session_key, callsign):
            self.location_calls.append((session_key, callsign))
            return QrzLocation(callsign=callsign, country="Spain", grid="IM99", lat=41.0, lon=2.0)

        self._orig_upload = appmod.upload_card_image
        self._orig_get_session_key = appmod.get_session_key
        self._orig_lookup_location = appmod.lookup_location
        self._orig_token = appmod.EQSL_IMPORT_TOKEN
        self._orig_qrz_user = appmod.QRZ_USERNAME
        self._orig_qrz_pass = appmod.QRZ_PASSWORD

        appmod.upload_card_image = fake_upload_card_image
        appmod.get_session_key = fake_get_session_key
        appmod.lookup_location = fake_lookup_location
        appmod.EQSL_IMPORT_TOKEN = "sekrit-token-123"
        appmod.QRZ_USERNAME = "joshuser"
        appmod.QRZ_PASSWORD = "joshpass"

    def tearDown(self):
        self.bucket.uninstall()
        appmod.upload_card_image = self._orig_upload
        appmod.get_session_key = self._orig_get_session_key
        appmod.lookup_location = self._orig_lookup_location
        appmod.EQSL_IMPORT_TOKEN = self._orig_token
        appmod.QRZ_USERNAME = self._orig_qrz_user
        appmod.QRZ_PASSWORD = self._orig_qrz_pass

    def post(self, token="sekrit-token-123", **fields):
        data = {"callsign": "", "image": fake_image(), **fields}
        headers = {"X-Eqsl-Import-Token": token} if token is not None else {}
        return self.client.post(
            "/admin/photomap/api/import-eqsl", data=data, headers=headers,
            content_type="multipart/form-data",
        )

    def test_requires_token(self):
        self.assertEqual(self.post(token=None, callsign="EA3FAB").status_code, 401)
        self.assertEqual(self.post(token="wrong", callsign="EA3FAB").status_code, 401)

    def test_unconfigured_token_returns_503_not_401(self):
        # Distinguishable from "wrong token" -- a 401 here would look
        # like the token itself is wrong instead of simply never set.
        appmod.EQSL_IMPORT_TOKEN = ""
        resp = self.post(token="anything", callsign="EA3FAB")
        self.assertEqual(resp.status_code, 503)

    def test_requires_callsign_and_image(self):
        resp = self.client.post(
            "/admin/photomap/api/import-eqsl",
            data={"image": fake_image()},
            headers={"X-Eqsl-Import-Token": "sekrit-token-123"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

        resp = self.client.post(
            "/admin/photomap/api/import-eqsl",
            data={"callsign": "N2LEE"},
            headers={"X-Eqsl-Import-Token": "sekrit-token-123"},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)

    def test_full_qso_info_looks_up_location_for_a_new_callsign(self):
        resp = self.post(
            callsign="EA3FAB", qso_date="20261109", band="12M", mode="FT8",
            freq="24.9169", rst_sent="-06", rst_rcvd="-24", note="via DigiQSL",
        )
        self.assertEqual(resp.status_code, 200, resp.get_json())
        body = resp.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["callsign"], "EA3FAB")
        self.assertTrue(body["has_location"])
        self.assertEqual(len(self.session_key_calls), 1)
        self.assertEqual(len(self.location_calls), 1)

        card = photomap_store.get_photo_card(body["card_id"])
        self.assertEqual(card["qso_date"], "20261109")
        self.assertEqual(card["band"], "12M")
        self.assertEqual(card["mode"], "FT8")
        self.assertEqual(card["note"], "via DigiQSL")
        self.assertEqual(len(card["images"]), 1)
        loc = photomap_store.get_callsign_location("EA3FAB")
        self.assertEqual(loc["country"], "Spain")

    def test_repeat_callsign_reuses_cached_location(self):
        self.post(callsign="EA3FAB", qso_date="20261109")
        self.post(callsign="EA3FAB", qso_date="20261110", band="10M")
        self.assertEqual(len(self.session_key_calls), 1, "should not re-hit QRZ for a cached callsign")

    def test_repeat_callsign_reuses_the_same_card_not_a_duplicate(self):
        first = self.post(callsign="EA3FAB", qso_date="20261109", band="12M")
        self.assertFalse(first.get_json()["reused_existing_card"])
        card_id = first.get_json()["card_id"]

        second = self.post(callsign="EA3FAB", qso_date="20261110", band="10M", note="second one")
        self.assertTrue(second.get_json()["reused_existing_card"])
        self.assertEqual(second.get_json()["card_id"], card_id, "must land on the same card, not a new one")

        cards = photomap_store.get_cards_for_callsign("EA3FAB")
        self.assertEqual(len(cards), 1, "exactly one card for this callsign")
        card = cards[0]
        self.assertEqual(len(card["images"]), 2, "both photos attached to the one card")
        # Existing non-blank fields are not clobbered by the second import...
        self.assertEqual(card["qso_date"], "20261109")
        self.assertEqual(card["band"], "12M")
        # ...but a field that was blank gets filled in from the new one.
        self.assertEqual(card["note"], "second one")

    def test_blank_fields_get_filled_in_without_overwriting_set_ones(self):
        # First import: callsign-only, no QSO info at all (bureau.e-qsl.com style).
        self.post(callsign="KE9GKG")
        # Second import for the same callsign later brings real QSO details.
        resp = self.post(callsign="KE9GKG", qso_date="20261113", band="20M", mode="SSB")
        self.assertTrue(resp.get_json()["reused_existing_card"])

        cards = photomap_store.get_cards_for_callsign("KE9GKG")
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["qso_date"], "20261113")
        self.assertEqual(card["band"], "20M")
        self.assertEqual(card["mode"], "SSB")
        self.assertEqual(len(card["images"]), 2)

    def test_callsign_only_card_saves_without_qso_info(self):
        # The QSLMaster bureau relay (<call>@bureau.e-qsl.com) and World
        # Radio League notifications never include QSO specifics --
        # Josh's call was to add the card anyway, flagged the same way
        # a manual upload missing that info already is.
        resp = self.post(callsign="KE9GKG")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        card = photomap_store.get_photo_card(resp.get_json()["card_id"])
        self.assertEqual(card["qso_date"], "")
        self.assertEqual(card["band"], "")
        self.assertEqual(len(card["images"]), 1)

    def test_qrz_failure_does_not_fail_the_import(self):
        def failing_lookup_location(session_key, callsign):
            raise QrzError("simulated QRZ outage")
        appmod.lookup_location = failing_lookup_location

        resp = self.post(callsign="F5TGY", qso_date="20261109")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(resp.get_json()["has_location"])
        # Card and image still saved despite the QRZ hiccup.
        card = photomap_store.get_photo_card(resp.get_json()["card_id"])
        self.assertEqual(len(card["images"]), 1)

    def test_no_qrz_credentials_configured_skips_lookup_without_error(self):
        appmod.QRZ_USERNAME = ""
        appmod.QRZ_PASSWORD = ""
        resp = self.post(callsign="M7HET", qso_date="20261111")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        self.assertFalse(resp.get_json()["has_location"])
        self.assertEqual(len(self.session_key_calls), 0)

    def test_exempt_from_csrf_protection(self):
        # No session, no csrf_token field, no X-CSRF-Token header -- the
        # only thing authenticating this request is the import token.
        # Added to CSRF_EXEMPT_ENDPOINTS; this pins that it stays there.
        resp = self.post(callsign="N2LEE")
        self.assertNotEqual(resp.status_code, 400)

    def test_shows_up_on_the_manage_page(self):
        self.post(callsign="EA3FAB", qso_date="20261109")
        self.post(callsign="KE9GKG")
        with self.client.session_transaction() as sess:
            sess["is_admin"] = True
        # bare manage page now defaults to the "paper" filter, so look at
        # "all" explicitly -- these two cards are both electronic imports.
        resp = self.client.get("/admin/photomap/manage?type=all")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("EA3FAB", html)
        self.assertIn("KE9GKG", html)


if __name__ == "__main__":
    unittest.main()
