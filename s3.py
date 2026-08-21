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

import mimetypes
import os
import uuid

import boto3
from botocore.exceptions import ClientError

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
    except ClientError as exc:
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
    except ClientError as exc:
        raise S3Error(str(exc)) from exc
