"""Paper / Electronic / Hybrid card-type filtering (Josh's ask, right
before applying the eQSL-import zip): a card's type is never set by
hand -- it's derived from how its image(s) arrived (photomap_store.
card_photo_type()), and that's what the manage page and the public map
let you filter by.
"""
from __future__ import annotations

import io
import unittest

import photomap_store
from .conftest import sign_in_admin
from .support import app, appmod, MemoryBucket


def fake_image(filename="card.jpg"):
    return (io.BytesIO(b"\xff\xd8\xff\xe0fakejpegbytes"), filename)


class CardPhotoTypeUnitTests(unittest.TestCase):
    """Straight unit tests on the derivation itself -- no app, no store."""

    def _card(self, images):
        return {"images": images}

    def test_no_images_is_none(self):
        self.assertEqual(photomap_store.card_photo_type(self._card([])), "none")

    def test_all_paper(self):
        card = self._card([{"s3_key": "a", "source": "paper"}, {"s3_key": "b", "source": "paper"}])
        self.assertEqual(photomap_store.card_photo_type(card), "paper")

    def test_all_electronic(self):
        card = self._card([{"s3_key": "a", "source": "electronic"}])
        self.assertEqual(photomap_store.card_photo_type(card), "electronic")

    def test_mixed_is_hybrid(self):
        card = self._card([{"s3_key": "a", "source": "paper"}, {"s3_key": "b", "source": "electronic"}])
        self.assertEqual(photomap_store.card_photo_type(card), "hybrid")

    def test_legacy_bare_string_images_count_as_paper(self):
        # Every image saved before this field existed is a plain string
        # key in the stored JSON, not a {"s3_key", "source"} dict.
        card = self._card(["photocards/KN0BLE/old.jpg"])
        self.assertEqual(photomap_store.card_photo_type(card), "paper")


class CardPhotoTypeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.bucket = MemoryBucket()
        self.bucket.install()
        self.client = app.test_client()
        sign_in_admin(self.client)

        def fake_upload_card_image(file_storage, callsign):
            return f"photocards/{callsign.upper()}/fake-{file_storage.filename}.jpg"

        self._orig_upload = appmod.upload_card_image
        appmod.upload_card_image = fake_upload_card_image
        appmod.EQSL_IMPORT_TOKEN = "sekrit-token-123"
        appmod.QRZ_USERNAME = ""
        appmod.QRZ_PASSWORD = ""

    def tearDown(self):
        self.bucket.uninstall()
        appmod.upload_card_image = self._orig_upload

    def _seed_location(self, callsign):
        # Skip the QRZ-lookup branch of the manual upload route -- the
        # location already being on file is the normal "known callsign"
        # path, and what's under test here is the image source, not QRZ.
        photomap_store.save_callsign_location(_Loc(callsign))

    def _manual_upload(self, callsign, **fields):
        data = {"callsign": callsign, "images": fake_image(), "csrf_token": "tok", **fields}
        return self.client.post(
            "/admin/photomap/upload", data=data, content_type="multipart/form-data",
        )

    def _eqsl_import(self, callsign, **fields):
        data = {"callsign": callsign, "image": fake_image(), **fields}
        return self.client.post(
            "/admin/photomap/api/import-eqsl", data=data,
            headers={"X-Eqsl-Import-Token": "sekrit-token-123"},
            content_type="multipart/form-data",
        )

    def test_manual_upload_is_paper(self):
        self._seed_location("EA3FAB")
        resp = self._manual_upload("EA3FAB")
        self.assertEqual(resp.status_code, 302, resp.get_data(as_text=True))
        cards = photomap_store.get_cards_for_callsign("EA3FAB")
        self.assertEqual(len(cards), 1)
        self.assertEqual(photomap_store.card_photo_type(cards[0]), "paper")

    def test_eqsl_import_is_electronic(self):
        resp = self._eqsl_import("F5TGY", qso_date="20261109")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        card = photomap_store.get_photo_card(resp.get_json()["card_id"])
        self.assertEqual(photomap_store.card_photo_type(card), "electronic")

    def test_eqsl_onto_an_existing_paper_card_becomes_hybrid(self):
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        resp = self._eqsl_import("EA3FAB", qso_date="20261109")
        self.assertTrue(resp.get_json()["reused_existing_card"])
        cards = photomap_store.get_cards_for_callsign("EA3FAB")
        self.assertEqual(len(cards), 1)
        self.assertEqual(photomap_store.card_photo_type(cards[0]), "hybrid")

    def test_manage_page_filters_by_type(self):
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        self._eqsl_import("F5TGY")

        all_resp = self.client.get("/admin/photomap/manage?type=all")
        html = all_resp.get_data(as_text=True)
        self.assertIn("EA3FAB", html)
        self.assertIn("F5TGY", html)

        paper_resp = self.client.get("/admin/photomap/manage?type=paper")
        paper_html = paper_resp.get_data(as_text=True)
        self.assertIn("EA3FAB", paper_html)
        self.assertNotIn("F5TGY", paper_html)

        electronic_resp = self.client.get("/admin/photomap/manage?type=electronic")
        electronic_html = electronic_resp.get_data(as_text=True)
        self.assertIn("F5TGY", electronic_html)
        self.assertNotIn("EA3FAB", electronic_html)

        hybrid_resp = self.client.get("/admin/photomap/manage?type=hybrid")
        hybrid_html = hybrid_resp.get_data(as_text=True)
        self.assertNotIn("EA3FAB", hybrid_html)
        self.assertNotIn("F5TGY", hybrid_html)
        self.assertIn("No hybrid cards.", hybrid_html)

    def test_manage_page_defaults_to_paper_with_no_type_param(self):
        # A bare visit (no ?type= at all) now shows paper cards only --
        # Josh's ask: paper is the common case, "All" is one click away.
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        self._eqsl_import("F5TGY")

        resp = self.client.get("/admin/photomap/manage")
        html = resp.get_data(as_text=True)
        self.assertIn("EA3FAB", html)
        self.assertNotIn("F5TGY", html)

        resp = self.client.get("/admin/photomap/manage?type=all")
        html = resp.get_data(as_text=True)
        self.assertIn("EA3FAB", html)
        self.assertIn("F5TGY", html)

    def test_manage_page_has_no_hybrid_pill_but_the_link_still_works(self):
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        self._eqsl_import("EA3FAB")  # same callsign -> hybrid

        resp = self.client.get("/admin/photomap/manage")
        html = resp.get_data(as_text=True)
        self.assertNotIn(">Hybrid<", html)

        hybrid_resp = self.client.get("/admin/photomap/manage?type=hybrid")
        self.assertIn("EA3FAB", hybrid_resp.get_data(as_text=True))

    def test_manage_page_ignores_a_bogus_type_value(self):
        # Not "paper"/"electronic"/"hybrid" -- treated the same as no
        # filter at all, instead of erroring or silently showing nothing.
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        resp = self.client.get("/admin/photomap/manage?type=sideways")
        self.assertIn("EA3FAB", resp.get_data(as_text=True))

    def test_public_pins_api_filters_by_type(self):
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        self._eqsl_import("F5TGY")
        photomap_store.save_callsign_location(_Loc("F5TGY"))

        all_pins = self.client.get("/photomap/api/pins?type=all").get_json()["pins"]
        self.assertEqual({p["callsign"] for p in all_pins}, {"EA3FAB", "F5TGY"})

        paper_pins = self.client.get("/photomap/api/pins?type=paper").get_json()["pins"]
        self.assertEqual({p["callsign"] for p in paper_pins}, {"EA3FAB"})

        electronic_pins = self.client.get("/photomap/api/pins?type=electronic").get_json()["pins"]
        self.assertEqual({p["callsign"] for p in electronic_pins}, {"F5TGY"})

    def test_pins_api_defaults_to_paper_with_no_type_param(self):
        self._seed_location("EA3FAB")
        self._manual_upload("EA3FAB")
        self._eqsl_import("F5TGY")
        photomap_store.save_callsign_location(_Loc("F5TGY"))

        default_pins = self.client.get("/photomap/api/pins").get_json()["pins"]
        self.assertEqual({p["callsign"] for p in default_pins}, {"EA3FAB"})

        hybrid_pins = self.client.get("/photomap/api/pins?type=hybrid").get_json()["pins"]
        self.assertEqual(hybrid_pins, [])

    def test_public_photomap_page_renders_with_a_type_param(self):
        resp = self.client.get("/photomap?type=electronic")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Electronic", resp.data)


class _Loc:
    """Minimal stand-in for qrz.QrzLocation -- save_callsign_location()
    only reads these attributes."""

    def __init__(self, callsign, lat=41.0, lon=2.0, country="Spain", state=None, county=None, grid="IM99"):
        self.callsign = callsign
        self.lat = lat
        self.lon = lon
        self.country = country
        self.state = state
        self.county = county
        self.grid = grid


if __name__ == "__main__":
    unittest.main()
