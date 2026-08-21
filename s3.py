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
import mimetypes
import os
import uuid

import boto3
from botocore.exceptions import BotoCoreError, ClientError

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

    ext = os.path.splitext(file_storage.filename or "")[1].lower() or ".jpg"
    key = f"photocards/{callsign.upper()}/{uuid.uuid4().hex}{ext}"
    content_type = (
        file_storage.content_type
        or mimetypes.guess_type(file_storage.filename or "")[0]
        or "application/octet-stream"
    )
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
    """A short-lived, view-only URL for a private object."""
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
