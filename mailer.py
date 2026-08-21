"""
Minimal email sending for the "Request a QSL Card" feature -- notifies
Josh by email when a visitor asks for a card back. Uses Gmail's SMTP
with an app password rather than a third-party email API, since a
personal Gmail account easily covers the very low volume this needs.

Configured via environment variables (set in Render's dashboard --
never commit real values to the repo):
  GMAIL_ADDRESS      -- the Gmail account sending the notification
  GMAIL_APP_PASSWORD -- a Google "app password" for that account
                        (requires 2-Step Verification to be turned on;
                        see README for the exact steps)
  NOTIFY_EMAIL       -- where the notification should land (defaults to
                        GMAIL_ADDRESS if not set separately)

If any of these aren't configured, sending is skipped entirely -- the
request still gets saved to the database either way. That keeps the
form usable before the Gmail setup is done, and resilient if the
credentials ever go stale later.
"""
from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage


def _clean(text) -> str:
    """Collapse whitespace and strip newlines out of user-supplied text
    before it goes anywhere near an email header or body -- the main
    defense against header-injection via a crafted form submission."""
    return " ".join(str(text or "").split())


def send_qsl_request_email(callsign: str, note: str, contact_email: str) -> bool:
    """Send Josh a notification email for a QSL card request.

    Returns True if an email was actually sent, False if it was skipped
    (missing config) or failed outright. Never raises -- a broken mail
    setup should never break the visitor-facing form.
    """
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    notify_email = os.environ.get("NOTIFY_EMAIL") or gmail_address

    if not gmail_address or not gmail_app_password or not notify_email:
        return False

    callsign = _clean(callsign)[:20]
    note = _clean(note)[:500]
    contact_email = _clean(contact_email)[:200]

    msg = EmailMessage()
    msg["Subject"] = f"QSL card request from {callsign or 'unknown callsign'}"
    msg["From"] = gmail_address
    msg["To"] = notify_email
    if contact_email:
        msg["Reply-To"] = contact_email

    body_lines = [f"Callsign: {callsign or '(not given)'}"]
    if contact_email:
        body_lines.append(f"Their email: {contact_email}")
    if note:
        body_lines.append(f"Note: {note}")
    body_lines.append("")
    body_lines.append('Sent from the "Request a QSL Card" form on kn0ble.com.')
    msg.set_content("\n".join(body_lines))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(gmail_address, gmail_app_password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False
