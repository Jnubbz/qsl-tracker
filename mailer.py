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

import contextlib
import logging
import os
import smtplib
import socket
import sys
from email.message import EmailMessage

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _force_ipv4_dns():
    """Temporarily make socket.getaddrinfo() return only IPv4 (AF_INET)
    results.

    Render's containers (at least as of 2026-08) advertise an IPv6
    address but don't actually have a working outbound IPv6 route --
    connecting to a host that has an AAAA record (like smtp.gmail.com)
    fails immediately with `OSError: [Errno 101] Network is unreachable`
    rather than timing out, because the kernel already knows it has no
    route for that address family. smtplib/socket.create_connection()
    tries whichever addresses getaddrinfo() returns, in order, so this
    forces it to only ever see IPv4 addresses -- diagnosed by an actual
    traceback showing exactly this error on 2026-08-21."""
    real_getaddrinfo = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)

    socket.getaddrinfo = ipv4_only
    try:
        yield
    finally:
        socket.getaddrinfo = real_getaddrinfo


def _log(message: str) -> None:
    """Log via both the `logging` module and a plain, flushed print to
    stderr. Belt-and-suspenders: if something about how gunicorn/Render
    wires up Python's logging module is swallowing our handler output,
    a direct flushed print to the same stream still gets through."""
    logger.warning(message)
    print(message, file=sys.stderr, flush=True)


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
    print("QSL request email: send_qsl_request_email() called", file=sys.stderr, flush=True)

    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")
    notify_email = os.environ.get("NOTIFY_EMAIL") or gmail_address

    if not gmail_address or not gmail_app_password or not notify_email:
        missing = [
            name
            for name, val in [
                ("GMAIL_ADDRESS", gmail_address),
                ("GMAIL_APP_PASSWORD", gmail_app_password),
                ("NOTIFY_EMAIL/GMAIL_ADDRESS", notify_email),
            ]
            if not val
        ]
        _log(f"QSL request email skipped -- missing env var(s): {', '.join(missing)}")
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

    print(f"QSL request email: attempting SMTP send for callsign {callsign or '(none)'}...",
          file=sys.stderr, flush=True)
    try:
        with _force_ipv4_dns(), smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as smtp:
            smtp.login(gmail_address, gmail_app_password)
            smtp.send_message(msg)
        _log(f"QSL request email sent for callsign {callsign or '(none)'}")
        return True
    except Exception as exc:
        _log(f"QSL request email FAILED for callsign {callsign or '(none)'}: {exc!r}")
        logger.exception("Failed to send QSL request notification email")
        return False
