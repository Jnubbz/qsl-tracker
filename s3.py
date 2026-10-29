"""
S3 storage for QSL Photo Map card images.

Images are uploaded through the admin-only form (see app.py's
/admin/photomap/upload) as a normal multipart file upload, then relayed
straight through to S3 from inside the request -- no direct-from-browser
presigned upload, so there's no S3 bucket CORS configuration for Josh to
set up. Card photos are small (a phone snapshot of a QSL card), so this
is well within Render's request limits.

The bucket is kept private. The public /photomap page never gets a
permanent URL to an object -- it gets a short-lived presigned GET URL,
regenerated on every page render.

Reads AWS credentials from the standard boto3-recognized env vars
(AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY) -- no custom names needed for
those, boto3 picks them up on its own. S3_BUCKET and AWS_REGION are ours
to set (see render.yaml / README).
"""
from __future__ import annotations

import json
import os
import re
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Card photos are the only objects the public image route will stream,
# and the only objects delete_object() will remove. The log itself is
# photocards/_index.json -- same prefix, but not a photo, and it must
# never be downloadable or deletable through those paths.
CARD_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}
CARD_IMAGE_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}
_CARD_IMAGE_RE = re.compile(
    r"^photocards/(?P<callsign>.+)/([0-9a-f]{32})\.(jpg|jpeg|png|webp|gif)$",
    re.IGNORECASE,
)

# ClientError covers AWS *rejecting* a request (bad bucket, permission
# denied, etc). BotoCoreError is the broader base class that also
# covers boto3/botocore failing before a request even reaches AWS --
# most importantly NoCredentialsError, which is what's raised when
# AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY simply aren't set. That's a
# very real, very likely-to-happen misconfiguration (an env var not
# set yet on Render), and it is NOT a ClientError subclass -- catching
# only ClientError let it through uncaught and crashed the whole page.

BUCKET = os.environ.get("S3_BUCKET", "")
_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")

_client = None


class S3Error(Exception):
    """Raised when an upload or presign call to S3 fails."""


def is_card_image_key(key: str) -> bool:
    """True only for photocards/<callsign>/<32 hex>.<image ext>.

    The callsign may contain slashes (FP/KJ1V). Anything else under the
    prefix, including photocards/_index.json, is rejected. `..` path
    segments are rejected too."""
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
        return False
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    if any(part.lower() == "_index.json" for part in parts):
        return False
    match = _CARD_IMAGE_RE.match(key)
    if not match:
        return False
    callsign = match.group("callsign")
    if not callsign or callsign.startswith("/"):
        return False
    return True


def image_type_for_key(key: str) -> str:
    """Content type from the key's extension, ignoring whatever S3 stored."""
    ext = key.rsplit(".", 1)[-1].lower()
    return CARD_IMAGE_TYPES.get(ext, "application/octet-stream")


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("s3", region_name=_REGION) if _REGION else boto3.client("s3")
    return _client


def upload_card_image(file_storage, callsign: str) -> str:
    """Upload one Werkzeug FileStorage to S3 and return the object key.

    Keyed under photocards/<callsign>/<random>.<ext> so a station's
    photos sit together in the bucket, and a random suffix means two
    uploads with the same original filename (e.g. both named
    "IMG_0001.jpg") never collide or overwrite each other.
    """
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")

    ext = os.path.splitext(file_storage.filename or "")[1].lower().lstrip(".")
    if ext not in CARD_IMAGE_EXTENSIONS:
        raise S3Error("Card photos must be JPEG, PNG, WEBP, or GIF.")
    # The extension and content type come from the allowlist, not from
    # the browser's Content-Type, so an .html upload can't be stored
    # and later served from this site.
    key = f"photocards/{callsign.upper()}/{uuid.uuid4().hex}.{ext}"
    content_type = CARD_IMAGE_TYPES[ext]
    try:
        _get_client().upload_fileobj(
            file_storage,
            BUCKET,
            key,
            ExtraArgs={"ContentType": content_type},
        )
    except (ClientError, BotoCoreError) as exc:
        raise S3Error(str(exc)) from exc
    return key


def presigned_url(key: str, expires_in: int = 3600) -> str:
    """A short-lived, view-only URL for a private object.

    Currently unused -- app.py serves QSL card photos through
    get_object_bytes()/photomap_image() instead (see that function's
    docstring for why). Left in place in case a future need for a
    direct-to-S3 URL comes up and this bites the dust for a different
    reason next time."""
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")
    try:
        return _get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    except (ClientError, BotoCoreError) as exc:
        raise S3Error(str(exc)) from exc


def delete_object(key: str) -> None:
    """Delete a single card photo. Refuses any other key, including the
    JSON index, so a form field can't be pointed at the log."""
    if not is_card_image_key(key):
        raise S3Error("Refusing to delete an object that isn't a card photo.")
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")
    try:
        _get_client().delete_object(Bucket=BUCKET, Key=key)
    except (ClientError, BotoCoreError) as exc:
        raise S3Error(str(exc)) from exc


def get_json(key: str):
    """Fetch and parse a JSON object from the bucket, or None if it
    doesn't exist yet (a fresh bucket, or first run). Used by
    photomap_store.py to keep the QSL Photo Map's data durable without
    needing Render's (ephemeral, free-tier) disk at all."""
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")
    try:
        obj = _get_client().get_object(Bucket=BUCKET, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as exc:
        # Only ClientError (AWS actually responded) carries .response --
        # that's how a "the object doesn't exist yet" case is told apart
        # from a real failure below.
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise S3Error(str(exc)) from exc
    except BotoCoreError as exc:
        # No .response here -- this is boto3/botocore failing before a
        # request ever reached AWS (e.g. NoCredentialsError because
        # AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY aren't set).
        raise S3Error(str(exc)) from exc


def get_object_bytes(key: str) -> tuple[bytes, str]:
    """Fetch a raw object's bytes and content-type from the bucket.

    Used to serve QSL card photos straight through Flask (see
    /photomap/image/<path:key> in app.py) instead of handing the
    browser a presigned URL. Reason: presigned GET URLs for this
    bucket kept coming back SignatureDoesNotMatch in production even
    after multiple from-scratch IAM key rotations and character-by-
    character verification of the secret -- while ordinary signed
    requests through this same client (get_json/put_json above) always
    worked fine. Since those calls share one boto3 client and one set
    of credentials, that strongly suggested something between "URL
    generated" and "URL fetched by the browser" was mangling or
    otherwise not reproducing the exact bytes that were signed (e.g. a
    network intermediary), not that the credentials were actually bad.
    Proxying the object bytes through the same already-proven-working
    get_object call sidesteps presigned-URL signing entirely. See
    qsl-tracker-status.md in the project for the full debugging trail.
    """
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")
    try:
        obj = _get_client().get_object(Bucket=BUCKET, Key=key)
        body = obj["Body"].read()
        content_type = obj.get("ContentType") or "application/octet-stream"
        return body, content_type
    except ClientError as exc:
        raise S3Error(str(exc)) from exc
    except BotoCoreError as exc:
        raise S3Error(str(exc)) from exc


def put_json(key: str, data) -> None:
    """Write a JSON-serializable value to the bucket as an object."""
    if not BUCKET:
        raise S3Error("S3_BUCKET isn't configured.")
    try:
        _get_client().put_object(
            Bucket=BUCKET,
            Key=key,
            Body=json.dumps(data, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
    except (ClientError, BotoCoreError) as exc:
        raise S3Error(str(exc)) from exc
