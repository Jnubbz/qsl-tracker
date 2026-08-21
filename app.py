"""
QSL Tracker -- look up callsigns against QRZ and keep track of which
ones want a direct paper QSL card.

No accounts: each visitor gets an anonymous session id (stored in a
signed cookie) that scopes their results in SQLite. Visitors log in
with their own QRZ XML subscriber credentials; we exchange those for a
short-lived QRZ session key and never store the password. That QRZ
session key lives server-side (SQLite, keyed by the anonymous session
id) rather than in the cookie itself -- see db.py's auth_sessions table.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import secrets
import time
from datetime import date

from flask import Flask, Response, flash, redirect, render_template, request, session, url_for

import db
from adif import distinct_callsigns, parse_adif
from mailer import send_qsl_request_email
from qrz import QrzError, format_mailing_label, get_session_key, lookup_callsign

# Without this, mailer.py's logger.warning()/logger.exception() calls for
# the QSL request email (see /request-qsl below) wouldn't reliably show
# up in Render's Logs tab -- INFO/WARNING records need a configured
# handler. force=True re-configures the root logger even if gunicorn (or
# anything else) already attached its own handler first, so this always
# takes effect regardless of import order.
logging.basicConfig(level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
db.init_db()

# Cap how many callsigns we'll look up from a single ADIF upload in one
# request, so one big log file can't tie up the server or hammer QRZ.
MAX_LOOKUPS_PER_UPLOAD = 200

# An ADIF upload does its QRZ lookups synchronously, inside the request
# that's serving the page -- there's no background job queue. Render's
# reverse proxy kills requests that run too long (historically ~30s),
# and 200 sequential QRZ round-trips at even a few hundred ms each can
# blow past that on its own, before any deliberate pacing. So instead of
# trusting MAX_LOOKUPS_PER_UPLOAD alone, the loop below watches the
# clock and stops itself with time to spare, always returning a normal
# response rather than risking a hard proxy timeout that looks like the
# app crashed.
UPLOAD_TIME_BUDGET_SECONDS = 20

# A small pause between lookups so a big upload doesn't fire QRZ
# requests back-to-back as fast as the network allows -- QRZ doesn't
# publish a rate limit, but there's no reason to hammer it.
LOOKUP_DELAY_SECONDS = 0.2

# If QRZ starts erroring on every request (e.g. throttling, an outage,
# or the session key going bad mid-batch), stop after a few in a row
# instead of grinding through the rest of the list for no reason.
MAX_CONSECUTIVE_FAILURES = 5

# The "Request a QSL Card" form (embedded on kn0ble.com, posts here) is
# public and unauthenticated, so it gets its own light rate limit keyed
# off the same anonymous session id everything else uses.
QSL_REQUEST_RATE_LIMIT = 3
QSL_REQUEST_RATE_WINDOW_SECONDS = 600


@app.before_request
def ensure_session():
    if "session_id" not in session:
        session["session_id"] = secrets.token_urlsafe(24)
    db.purge_expired()


def current_auth():
    """The (qrz_key, qrz_username) row for this visitor, or None."""
    return db.get_auth(session["session_id"])


def qrz_key_or_none():
    auth = current_auth()
    return auth["qrz_key"] if auth else None


@app.route("/")
def index():
    return render_template("index.html", logged_in=bool(qrz_key_or_none()))


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("qrz_username", "").strip()
    password = request.form.get("qrz_password", "")
    if not username or not password:
        flash("Enter your QRZ XML subscriber username and password.", "error")
        return redirect(url_for("index"))

    try:
        key = get_session_key(username, password)
    except QrzError as exc:
        flash(f"QRZ login failed: {exc}", "error")
        return redirect(url_for("index"))
    except Exception:
        flash("Couldn't reach QRZ right now. Try again in a moment.", "error")
        return redirect(url_for("index"))

    # Only the short-lived session key is kept -- never the password --
    # and it's stored server-side, keyed by the anonymous session id,
    # rather than in the cookie itself.
    db.save_auth(session["session_id"], key, username)
    flash("Logged in to QRZ.", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    db.clear_auth(session["session_id"])
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    if not qrz_key_or_none():
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    direct_only = request.args.get("filter") == "direct"
    contacts = db.get_contacts(session["session_id"], direct_only=direct_only)
    auth = current_auth()
    return render_template(
        "dashboard.html",
        contacts=contacts,
        direct_only=direct_only,
        qrz_username=auth["qrz_username"] if auth else None,
    )


@app.route("/export.csv")
def export_csv():
    if not qrz_key_or_none():
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    # Mailing labels only make sense for contacts we actually kept an
    # address for, regardless of which filter the dashboard table
    # happens to be showing right now.
    contacts = db.get_contacts(session["session_id"], direct_only=True)
    if not contacts:
        flash("No direct-QSL contacts with an address to export yet.", "error")
        return redirect(url_for("dashboard"))

    # One cell per contact holds a full, ready-to-paste mailing label
    # (name, street, city/state/zip, country all on their own lines) --
    # select the cell, paste, done. Callsign stays a separate column
    # purely for reference/sorting; it's not part of the label itself.
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["Callsign", "Mailing Label"])
    for c in contacts:
        label = format_mailing_label(
            c["name"], c["address"], c["city"], c["state"], c["zip_code"], c["country"]
        )
        writer.writerow([c["callsign"], label])

    filename = f"qsl-direct-contacts-{date.today().isoformat()}.csv"
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/search", methods=["POST"])
def search():
    key = qrz_key_or_none()
    if not key:
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    callsign = request.form.get("callsign", "").strip()
    if not callsign:
        flash("Enter a callsign to look up.", "error")
        return redirect(url_for("dashboard"))

    try:
        record = lookup_callsign(key, callsign)
        db.upsert_contact(session["session_id"], record)
        flash(f"Looked up {record.callsign}.", "success")
    except QrzError as exc:
        flash(f"QRZ lookup failed: {exc}", "error")
    except Exception:
        flash("Couldn't reach QRZ right now. Try again in a moment.", "error")

    return redirect(url_for("dashboard"))


@app.route("/upload", methods=["POST"])
def upload():
    key = qrz_key_or_none()
    if not key:
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    file = request.files.get("adif_file")
    if not file or not file.filename:
        flash("Choose an ADIF (.adi) file to upload.", "error")
        return redirect(url_for("dashboard"))

    try:
        text = file.read().decode("utf-8", errors="ignore")
    except Exception:
        flash("Couldn't read that file.", "error")
        return redirect(url_for("dashboard"))

    qsos = parse_adif(text)
    callsigns = distinct_callsigns(qsos)[:MAX_LOOKUPS_PER_UPLOAD]

    if not callsigns:
        flash("No callsigns found in that file.", "error")
        return redirect(url_for("dashboard"))

    looked_up, failed = 0, 0
    consecutive_failures = 0
    stopped_early = None
    started_at = time.monotonic()

    for i, callsign in enumerate(callsigns):
        if time.monotonic() - started_at > UPLOAD_TIME_BUDGET_SECONDS:
            stopped_early = "ran out of time for this request"
            break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            stopped_early = "QRZ failed several times in a row"
            break

        try:
            record = lookup_callsign(key, callsign)
            db.upsert_contact(session["session_id"], record)
            looked_up += 1
            consecutive_failures = 0
        except QrzError:
            failed += 1
            consecutive_failures += 1
        except Exception:
            failed += 1
            consecutive_failures += 1

        if i < len(callsigns) - 1:
            time.sleep(LOOKUP_DELAY_SECONDS)

    processed = looked_up + failed
    message = f"Looked up {looked_up} of {len(callsigns)} callsigns from your log."
    if failed:
        message += f" ({failed} failed or had no QRZ record.)"
    if stopped_early:
        remaining = len(callsigns) - processed
        message += (
            f" Stopped after {processed} because {stopped_early}"
            f" -- {remaining} callsign{'s' if remaining != 1 else ''} left."
            " Upload the log again to pick up where this left off"
            " (already-found contacts won't need re-fetching to show up,"
            " but will be looked up again too)."
        )
    flash(message, "success" if looked_up else "error")
    return redirect(url_for("dashboard"))


@app.route("/clear", methods=["POST"])
def clear():
    db.clear_session(session["session_id"])
    flash("Cleared your results.", "success")
    return redirect(url_for("dashboard"))


@app.route("/request-qsl", methods=["GET", "POST"])
def request_qsl():
    """Public, unauthenticated form (embedded on kn0ble.com) for a
    visitor to ask for a QSL card back -- no QRZ login required. On
    submit it's saved to the database and, if Resend is configured
    (see mailer.py), emailed straight to Josh."""
    if request.method == "POST":
        # Honeypot: a hidden field real visitors never see or fill in.
        # A bot that fills every field trips this -- pretend success
        # without sending an email or storing anything, so it doesn't
        # even learn the trick failed.
        if request.form.get("website"):
            logger.warning(
                "QSL request dropped -- honeypot field was filled (likely a bot, "
                "or a password manager / autofill extension filling a hidden field)"
            )
            return redirect(url_for("request_qsl_thanks"))

        callsign = request.form.get("callsign", "").strip().upper()[:20]
        note = request.form.get("note", "").strip()[:500]
        contact_email = request.form.get("email", "").strip()[:200]

        if not callsign:
            flash("Enter a callsign so I know who to send a card to.", "error")
            return redirect(url_for("request_qsl"))

        session_id = session["session_id"]
        if db.count_recent_qsl_requests(session_id, QSL_REQUEST_RATE_WINDOW_SECONDS) >= QSL_REQUEST_RATE_LIMIT:
            flash("Too many requests from this browser recently -- try again later.", "error")
            return redirect(url_for("request_qsl"))

        db.save_qsl_request(session_id, callsign, note, contact_email)
        logger.info("QSL request saved for callsign %s, sending notification email...", callsign)
        send_qsl_request_email(callsign, note, contact_email)
        return redirect(url_for("request_qsl_thanks"))

    return render_template("request_qsl.html")


@app.route("/request-qsl/thanks")
def request_qsl_thanks():
    return render_template("request_qsl_thanks.html")


if __name__ == "__main__":
    app.run(debug=True)
