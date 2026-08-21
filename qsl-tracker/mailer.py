"""
Minimal email sending for the "Request a QSL Card" feature -- notifies
Josh by email when a visitor asks for a card back.

Uses Resend's HTTP API (https://resend.com) rather than raw SMTP.
Originally this used Gmail SMTP, but Render's free plan silently blocks
outbound SMTP entirely -- both port 465 (implicit TLS) and port 587
(STARTTLS) reliably timed out from a live deployment (confirmed
2026-08-21), which is a known anti-spam policy on a lot of free-tier
hosts. Plain HTTPS (port 443) isn't blocked -- the app already depends
on it for the QRZ API -- so an HTTP-based email API sidesteps the
problem entirely. Uses `requests`, already a dependency for the QRZ
client.

Configured via environment variables (set in Render's dashboard --
never commit real values to the repo):
  RESEND_API_KEY -- API key from resend.com (free tier is plenty for
                    this volume; no credit card required)
  NOTIFY_EMAIL   -- where the notification should land, e.g. Josh's own
                    Gmail address. On a resend.com account that hasn't
                    verified a custom sending domain, this MUST be the
                    same email address the Resend account was created
                    with -- their free/unverified tier only allows
                    sending to your own address, as an anti-abuse
                    measure. Verifying a domain (e.g. kn0ble.com) lifts
                    that restriction if it's ever needed.

If either of these aren't configured, sending is skipped entirely --
the request still gets saved to the database either way. That keeps
the form usable before the Resend setup is done, and resilient if the
API key ever goes stale later.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Resend's sandbox "from" address -- works without verifying a custom
# domain, as long as NOTIFY_EMAIL is the Resend account's own address.
DEFAULT_FROM = "QSL Tracker <onboarding@resend.dev>"


def _clean(text) -> str:
    """Collapse whitespace and strip newlines out of user-supplied text
    before it goes anywhere near an email subject/body -- defense
    against injection via a crafted form submission."""
    return " ".join(str(text or "").split())


def send_qsl_request_email(callsign: str, note: str, contact_email: str) -> bool:
    """Send Josh a notification email for a QSL card request via the
    Resend HTTP API.

    Returns True if the API accepted the email, False if it was skipped
    (missing config) or the request failed outright. Never raises -- a
    broken mail setup should never break the visitor-facing form.
    """
    api_key = os.environ.get("RESEND_API_KEY")
    notify_email = os.environ.get("NOTIFY_EMAIL")

    if not api_key or not notify_email:
        missing = [
            name
            for name, val in [("RESEND_API_KEY", api_key), ("NOTIFY_EMAIL", notify_email)]
            if not val
        ]
        logger.warning("QSL request email skipped -- missing env var(s): %s", ", ".join(missing))
        return False

    callsign = _clean(callsign)[:20]
    note = _clean(note)[:500]
    contact_email = _clean(contact_email)[:200]

    body_lines = [f"Callsign: {callsign or '(not given)'}"]
    if contact_email:
        body_lines.append(f"Their email: {contact_email}")
    if note:
        body_lines.append(f"Note: {note}")
    body_lines.append("")
    body_lines.append('Sent from the "Request a QSL Card" form on kn0ble.com.')

    payload = {
        "from": DEFAULT_FROM,
        "to": [notify_email],
        "subject": f"QSL card request from {callsign or 'unknown callsign'}",
        "text": "\n".join(body_lines),
    }
    if contact_email:
        payload["reply_to"] = contact_email

    try:
        resp = requests.post(
            RESEND_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(
                "QSL request email FAILED for callsign %s: Resend returned %s: %s",
                callsign or "(none)", resp.status_code, resp.text[:500],
            )
            return False
        logger.info("QSL request email sent for callsign %s", callsign or "(none)")
        return True
    except Exception:
        logger.exception("QSL request email FAILED for callsign %s", callsign or "(none)")
        return False
