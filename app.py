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
from functools import wraps

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for

import db
import labels
import photomap_store
from adif import distinct_callsigns, parse_adif
from mailer import send_qsl_request_email
from qrz import (
    QrzError,
    QrzLogbookError,
    call_option,
    fetch_logged_qsos,
    fetch_raw,
    fetch_recent_qsos,
    format_mailing_label,
    get_session_key,
    lookup_callsign,
    lookup_location,
    recent_option,
)
from s3 import S3Error, delete_object, get_object_bytes, upload_card_image

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

# Gates the QSL Photo Map's upload/import pages -- just Josh, not the
# QRZ-login system the rest of the app uses (that's per-visitor and
# anonymous; this is one person's admin area). Unset in an environment
# that hasn't configured it yet -- see admin_login() below.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# The QRZ Logbook API's own per-logbook access key (from Josh's QRZ
# account: Logbook -> Settings -> API) -- a completely different secret
# from the QRZ username/password the admin-login/dashboard flows use.
# Powers the QSO-label page's live fetch straight from Josh's QRZ
# Logbook -- see qrz.fetch_logged_qsos() and admin_qso_label() below.
QRZ_LOGBOOK_API_KEY = os.environ.get("QRZ_LOGBOOK_API_KEY", "")


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


def qrz_login_attempt(username: str, password: str) -> str | None:
    """Shared by /login (the main site's per-visitor QRZ login) and
    /admin/login's QRZ step (see below) so both handle a failed QRZ
    login the same way. Flashes an error and returns None on failure;
    on success, stores the session key (never the password) and
    returns it."""
    username = username.strip()
    if not username or not password:
        flash("Enter your QRZ XML subscriber username and password.", "error")
        return None
    try:
        key = get_session_key(username, password)
    except QrzError as exc:
        flash(f"QRZ login failed: {exc}", "error")
        return None
    except Exception:
        flash("Couldn't reach QRZ right now. Try again in a moment.", "error")
        return None
    db.save_auth(session["session_id"], key, username)
    return key


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Log in as admin first.", "error")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def s3_config_hint(exc: S3Error) -> str:
    """A friendlier message for an S3Error surfaced to the admin. The
    QSL Photo Map's data lives entirely in S3 now (see photomap_store.py)
    -- the most common cause of a failure here is one of the AWS env
    vars being missing or wrong on Render, not a real S3 outage."""
    logger.warning("QSL Photo Map S3 error: %s", exc)
    return (
        f"Couldn't reach S3 for the QSL Photo Map's data ({exc}). Check "
        "S3_BUCKET, AWS_REGION, AWS_ACCESS_KEY_ID, and "
        "AWS_SECRET_ACCESS_KEY are all set correctly in Render's "
        "Environment settings."
    )


@app.route("/")
def index():
    return render_template("index.html", logged_in=bool(qrz_key_or_none()))


@app.route("/login", methods=["POST"])
def login():
    key = qrz_login_attempt(
        request.form.get("qrz_username", ""), request.form.get("qrz_password", "")
    )
    if key is None:
        return redirect(url_for("index"))
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


# ---------------------------------------------------------------------
# QSL Photo Map -- admin (Josh-only) upload/import, plus the public map.
# ---------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    """Two steps, QRZ first: the QSL Photo Map's upload flow needs a
    QRZ session (to look up a new callsign's map location) as often as
    it needs the admin password itself, and discovering that need
    *mid-upload* -- after already getting past the password -- was the
    original annoyance. So if there's no QRZ session yet, that's asked
    for first; only once it's in place (or was already there from an
    earlier /login) does the password step show. Both steps land back
    on `next` when done, and a QRZ session already established via the
    main site's /login skips the QRZ step here entirely."""
    next_url = request.values.get("next") or url_for("admin_photomap_upload")

    if request.method == "POST" and "qrz_username" in request.form:
        key = qrz_login_attempt(
            request.form.get("qrz_username", ""), request.form.get("qrz_password", "")
        )
        if key is None:
            return render_template("admin_login.html", next=next_url, stage="qrz")
        flash("Logged in to QRZ.", "success")
        # Fall through to the password step below -- no redirect needed,
        # qrz_key_or_none() will now see the session just saved.

    elif request.method == "POST" and "password" in request.form:
        password = request.form.get("password", "")
        if not ADMIN_PASSWORD:
            flash("Admin login isn't configured yet (ADMIN_PASSWORD isn't set on the server).", "error")
            return render_template("admin_login.html", next=next_url, stage="password")
        if not secrets.compare_digest(password, ADMIN_PASSWORD):
            flash("Wrong password.", "error")
            return render_template("admin_login.html", next=next_url, stage="password")
        session["is_admin"] = True
        flash("Logged in.", "success")
        return redirect(next_url)

    if not qrz_key_or_none():
        return render_template("admin_login.html", next=next_url, stage="qrz")
    return render_template("admin_login.html", next=next_url, stage="password")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


@app.route("/admin/label")
@admin_required
def admin_label():
    """Ad-hoc QSL mailing-label lookup. Looks up one callsign against QRZ
    (reusing the same lookup_callsign() address-eligibility gating as the
    main dashboard -- see qrz.py) and, if there's an address on file for
    a direct card, shows an on-screen preview plus a link to a print-ready
    PDF (see /admin/label/pdf and labels.py) sized for one cell of an
    Avery 8160/5160/5260 1"x2-5/8" address-label sheet."""
    callsign = request.args.get("callsign", "").strip().upper()
    position = labels.clamp_mailing_position(_parse_int(request.args.get("position"), default=1))
    record = None
    error = None

    if callsign:
        key = qrz_key_or_none()
        if not key:
            return redirect(
                url_for(
                    "admin_login",
                    next=url_for("admin_label", callsign=callsign, position=position),
                )
            )
        try:
            record = lookup_callsign(key, callsign)
            if not record.accepts_direct:
                error = (
                    f"No mailing address on file for {record.callsign} that they've "
                    "made available for a direct card -- either QRZ has no address, "
                    "they've opted out of direct QSLs, or cards should route through "
                    "a QSL manager instead."
                )
                record = None
        except QrzError as exc:
            error = f"QRZ lookup failed: {exc}"
        except Exception:
            error = "Couldn't reach QRZ right now. Try again in a moment."

    return render_template(
        "admin_label.html",
        callsign=callsign,
        position=position,
        label_count=labels.MAILING_LABEL_COUNT,
        record=record,
        label_lines=labels.label_lines(record) if record else [],
        error=error,
    )


@app.route("/admin/label/pdf")
@admin_required
def admin_label_pdf():
    callsign = request.args.get("callsign", "").strip().upper()
    position = labels.clamp_mailing_position(_parse_int(request.args.get("position"), default=1))
    if not callsign:
        abort(400)

    key = qrz_key_or_none()
    if not key:
        abort(401)

    try:
        record = lookup_callsign(key, callsign)
    except QrzError:
        abort(404)
    except Exception:
        abort(502)

    if not record.accepts_direct:
        abort(404)

    pdf_bytes = labels.generate_mailing_label_pdf(record, position)
    filename = f"qsl-label-{record.callsign}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _parse_int(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def _qso_key(qso: dict) -> str:
    """A stable-enough identifier for one QSO within a single callsign's
    match list, used instead of a database id -- there's no persisted
    store to hand one out anymore (see admin_qso_label() below), so the
    "which QSO did you pick" step re-fetches from QRZ and re-matches by
    this composite of date/time/band/mode/freq instead. Two QSOs with
    the same callsign colliding on all five of those is not realistic
    for a ham log."""
    return "|".join([
        qso.get("qso_date", ""), qso.get("time_on", ""), qso.get("band", ""),
        qso.get("mode", ""), qso.get("freq", ""),
    ])


def _rows_from_adif(adif_text: str) -> list[dict]:
    """Turn QRZ Logbook API ADIF text into the row shape the old
    ADIF-import store used to produce (qso_date/time_on/band/mode/freq/
    rst_sent/rst_rcvd) plus a `key` for _qso_key() above, newest first.
    Shared by _fetch_qso_matches() and _fetch_recent_qsos() below."""
    rows = []
    for qso in parse_adif(adif_text):
        f = qso.fields
        row = {
            "callsign": qso.callsign,
            "qso_date": f.get("qso_date", ""),
            "time_on": f.get("time_on", ""),
            "band": f.get("band", "").upper(),
            "mode": f.get("mode", "").upper(),
            "freq": f.get("freq", ""),
            "rst_sent": f.get("rst_sent", ""),
            "rst_rcvd": f.get("rst_rcvd", ""),
        }
        row["key"] = _qso_key(row)
        rows.append(row)
    rows.sort(key=lambda r: (r.get("qso_date") or "", r.get("time_on") or ""), reverse=True)
    return rows


def _fetch_qso_matches(callsign: str) -> list[dict]:
    """Every QSO with `callsign` in Josh's own QRZ Logbook, newest
    first. Raises QrzLogbookError if the API rejects the request (e.g.
    a bad/missing key)."""
    return _rows_from_adif(fetch_logged_qsos(QRZ_LOGBOOK_API_KEY, callsign))


def _fetch_recent_qsos(days: int = 30, max_results: int = 25) -> list[dict]:
    """Diagnostic fallback for admin_qso_label(): the last `days` days
    of QSOs under ANY callsign, regardless of the CALL: filter -- see
    qrz.fetch_recent_qsos() for why. Raises QrzLogbookError the same way
    _fetch_qso_matches() does; callers should decide whether to surface
    that or just quietly skip showing the diagnostic list."""
    return _rows_from_adif(fetch_recent_qsos(QRZ_LOGBOOK_API_KEY, days, max_results))


@app.route("/admin/qso-label")
@admin_required
def admin_qso_label():
    """Print a label of one of Josh's own logged QSOs (callsign,
    date/time in UTC, band/mode/freq, RST sent/received) -- meant to be
    stuck onto a blank 2"x4" spot on the physical QSL card instead of
    hand-written. Fetched live from Josh's own QRZ Logbook via the QRZ
    Logbook API (see qrz.fetch_logged_qsos()) -- never the XML Data API
    used elsewhere in this app, which only knows a station's own
    profile, not the specifics of a contact with them. Needs
    QRZ_LOGBOOK_API_KEY configured; doesn't need or use a visitor QRZ
    login/session at all -- the Logbook API's key is a standing secret,
    not a per-visitor credential.

    A callsign can have several logged QSOs (repeat contacts, different
    bands/dates), so this is a two-step page: first pick a callsign and
    see the matches, then pick the specific QSO (`qso_key`) to preview
    and print."""
    callsign = request.args.get("callsign", "").strip().upper()
    qso_key = request.args.get("qso_key", "")
    position = labels.clamp_qso_position(_parse_int(request.args.get("position"), default=1))

    debug = request.args.get("debug") == "1"

    matches = []
    qso = None
    error = None
    recent = []
    recent_note = None
    raw_call = None
    raw_recent = None

    if not QRZ_LOGBOOK_API_KEY:
        error = "QRZ Logbook API isn't configured yet (QRZ_LOGBOOK_API_KEY isn't set on the server)."
    elif callsign:
        try:
            matches = _fetch_qso_matches(callsign)
        except QrzLogbookError as exc:
            error = str(exc)
        except Exception:
            error = "Couldn't reach QRZ's Logbook API right now. Try again in a moment."

        if not error and not matches:
            error = (
                f"No logged QSO with {callsign} found in your QRZ Logbook via the API. "
                "If you just logged this, QRZ's API can occasionally lag a few minutes "
                "behind the website -- try again shortly. The list below shows what your "
                "API key can see right now, in case it's under a slightly different call."
            )
            # Diagnostic -- surface exactly what happened (empty-but-OK vs.
            # an outright failure) rather than just logging it server-side,
            # since Josh can't see Render's logs from the page itself.
            try:
                recent = _fetch_recent_qsos()
                if not recent:
                    recent_note = (
                        "Checked: your API key shows zero QSOs logged in the last 30 days, "
                        "under ANY callsign. If that doesn't match what you see on QRZ's own "
                        "logbook page, this API key is most likely scoped to a different "
                        "logbook/callsign than the one you're viewing on qrz.com -- worth "
                        "checking Logbook -> Settings -> API on QRZ to confirm which logbook "
                        "this key belongs to."
                    )
            except QrzLogbookError as exc:
                logger.warning("QRZ Logbook recent-QSO diagnostic fetch failed: %s", exc)
                recent_note = f"Couldn't check recent QSOs either -- QRZ said: {exc}"
            except Exception as exc:
                logger.warning("QRZ Logbook recent-QSO diagnostic fetch failed: %s", exc)
                recent_note = f"Couldn't check recent QSOs either ({type(exc).__name__}: {exc})."

            # Raw-response debug view, opt-in via ?debug=1 -- shows the
            # literal, completely unparsed bytes QRZ sent back, so a "the
            # API genuinely has nothing" conclusion can be checked
            # against the actual response rather than this module's
            # interpretation of it. The CALL: request is fired three
            # times in a row (a couple seconds apart) rather than once --
            # a single debug fetch previously came back RESULT=OK,
            # COUNT=1 with a real, fully-parseable QSO, while the normal
            # search page (same exact request, confirmed via a hard
            # refresh so it isn't a browser-cache artifact) kept
            # reporting zero matches moments later. Repeating the request
            # here is how to tell "QRZ is genuinely inconsistent between
            # near-simultaneous identical calls" apart from "that one
            # lucky response was a fluke that won't reproduce" -- both
            # are real possibilities and neither should be assumed.
            if debug:
                raw_call_attempts = []
                for attempt in range(3):
                    try:
                        raw_call_attempts.append(fetch_raw(QRZ_LOGBOOK_API_KEY, call_option(callsign)))
                    except Exception as exc:
                        raw_call_attempts.append(f"(raw fetch itself failed: {type(exc).__name__}: {exc})")
                    if attempt < 2:
                        time.sleep(1.5)
                raw_call = raw_call_attempts
                try:
                    raw_recent = fetch_raw(QRZ_LOGBOOK_API_KEY, recent_option())
                except Exception as exc:
                    raw_recent = f"(raw fetch itself failed: {type(exc).__name__}: {exc})"
        elif not error and qso_key:
            qso = next((m for m in matches if m["key"] == qso_key), None)
            if qso is None:
                error = "That logged QSO couldn't be found -- your QRZ Logbook may have changed."
            else:
                # A specific QSO was picked -- show its preview, not the
                # full match table again.
                matches = []

    # This page hits QRZ's live Logbook API on every load -- caching it
    # (browser back/forward cache, a shared proxy, etc.) risks showing a
    # stale "not found" result even after the underlying QRZ data has
    # changed, which is exactly the kind of confusion this route can't
    # afford given how much of it depends on being genuinely live.
    response = Response(render_template(
        "admin_qso_label.html",
        callsign=callsign,
        position=position,
        label_count=labels.QSO_LABEL_COUNT,
        matches=matches,
        qso=qso,
        qso_label_lines=labels.qso_label_lines(qso) if qso else [],
        error=error,
        recent=recent,
        recent_note=recent_note,
        debug=debug,
        raw_call=raw_call,
        raw_recent=raw_recent,
    ))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/admin/qso-label/pdf")
@admin_required
def admin_qso_label_pdf():
    callsign = request.args.get("callsign", "").strip().upper()
    qso_key = request.args.get("qso_key", "")
    position = labels.clamp_qso_position(_parse_int(request.args.get("position"), default=1))
    if not callsign or not qso_key:
        abort(400)

    if not QRZ_LOGBOOK_API_KEY:
        abort(503)

    try:
        matches = _fetch_qso_matches(callsign)
    except QrzLogbookError as exc:
        logger.warning("QRZ Logbook error: %s", exc)
        abort(502)
    except Exception:
        abort(502)

    qso = next((m for m in matches if m["key"] == qso_key), None)
    if qso is None:
        abort(404)

    pdf_bytes = labels.generate_qso_label_pdf(qso, position)
    filename = f"qso-label-{qso['callsign']}-{qso.get('qso_date', '')}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/photomap/import-adif", methods=["GET", "POST"])
@admin_required
def admin_import_adif():
    """Upload Josh's own ADIF log so the photo-upload form below can
    offer to auto-fill QSO details for a callsign. Separate from the
    main dashboard's /upload -- that one drives QRZ lookups per visitor
    and doesn't keep QSO details; this one keeps the QSO details
    (date/band/mode/freq/RST) and never touches QRZ."""
    if request.method == "POST":
        file = request.files.get("adif_file")
        if not file or not file.filename:
            flash("Choose an ADIF (.adi) file to upload.", "error")
            return redirect(url_for("admin_import_adif"))
        try:
            text = file.read().decode("utf-8", errors="ignore")
        except Exception:
            flash("Couldn't read that file.", "error")
            return redirect(url_for("admin_import_adif"))

        qsos = parse_adif(text)
        try:
            added, backfilled = photomap_store.import_my_qsos(qsos)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(url_for("admin_import_adif"))
        message = f"Imported {added} new QSO record(s) ({len(qsos)} found in the file)."
        if backfilled:
            message += f" Backfilled a UTC time onto {backfilled} already-imported record(s)."
        flash(message, "success")
        return redirect(url_for("admin_import_adif"))

    try:
        qso_count = photomap_store.count_my_qsos()
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        qso_count = 0
    return render_template("admin_import_adif.html", qso_count=qso_count)


@app.route("/admin/photomap/api/qsos")
@admin_required
def admin_photomap_api_qsos():
    """JSON list of Josh's logged QSOs for one callsign, used by the
    upload form's JS to offer auto-filling date/band/mode/freq/RST."""
    callsign = request.args.get("callsign", "").strip().upper()
    if not callsign:
        return {"qsos": []}
    try:
        rows = photomap_store.find_my_qsos(callsign)
    except S3Error as exc:
        logger.warning("QSL Photo Map S3 error: %s", exc)
        return {"qsos": [], "error": str(exc)}
    return {"qsos": [dict(r) for r in rows]}


@app.route("/admin/photomap/upload", methods=["GET", "POST"])
@admin_required
def admin_photomap_upload():
    if request.method == "POST":
        callsign = request.form.get("callsign", "").strip().upper()
        if not callsign:
            flash("Enter a callsign.", "error")
            return redirect(url_for("admin_photomap_upload"))

        files = [f for f in request.files.getlist("images") if f and f.filename]
        if not files:
            flash("Choose at least one photo of the card front.", "error")
            return redirect(url_for("admin_photomap_upload", callsign=callsign))

        qso_date = request.form.get("qso_date", "").strip()
        band = request.form.get("band", "").strip().upper()
        mode = request.form.get("mode", "").strip().upper()
        freq = request.form.get("freq", "").strip()
        rst_sent = request.form.get("rst_sent", "").strip()
        rst_rcvd = request.form.get("rst_rcvd", "").strip()
        note = request.form.get("note", "").strip()

        # Location is cached per callsign so a repeat upload for the
        # same station doesn't re-hit QRZ. The admin needs a QRZ
        # session to populate it the first time -- the same login used
        # everywhere else in the app (see /login).
        try:
            location = photomap_store.get_callsign_location(callsign)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(url_for("admin_photomap_upload", callsign=callsign))

        if location is None:
            key = qrz_key_or_none()
            if not key:
                flash(f"Log in with QRZ so {callsign}'s location can be looked up, then try again.", "error")
                return redirect(
                    url_for("admin_login", next=url_for("admin_photomap_upload", callsign=callsign))
                )
            try:
                loc = lookup_location(key, callsign)
            except QrzError as exc:
                flash(f"QRZ lookup failed: {exc}", "error")
                return redirect(url_for("admin_photomap_upload", callsign=callsign))
            except Exception:
                flash("Couldn't reach QRZ right now. Try again in a moment.", "error")
                return redirect(url_for("admin_photomap_upload", callsign=callsign))
            try:
                photomap_store.save_callsign_location(loc)
                location = photomap_store.get_callsign_location(callsign)
            except S3Error as exc:
                flash(s3_config_hint(exc), "error")
                return redirect(url_for("admin_photomap_upload", callsign=callsign))

        try:
            card_id = photomap_store.add_photo_card(callsign, qso_date, band, mode, freq, rst_sent, rst_rcvd, note)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(url_for("admin_photomap_upload", callsign=callsign))

        uploaded, failed = 0, 0
        for f in files:
            try:
                key = upload_card_image(f, callsign)
                photomap_store.add_photo_card_image(card_id, key)
                uploaded += 1
            except S3Error:
                failed += 1

        message = f"Saved {callsign} with {uploaded} photo(s)."
        if location and (location["lat"] is None or location["lon"] is None):
            message += " QRZ has no grid/lat-lon on file for this station, so it won't show up on the map yet."
        if failed:
            message += f" {failed} image(s) failed to upload to S3."
        flash(message, "success" if uploaded else "error")
        return redirect(url_for("admin_photomap_upload"))

    try:
        recent_cards = photomap_store.list_recent_photo_cards(10)
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        recent_cards = []
    return render_template(
        "admin_photomap_upload.html",
        callsign_prefill=request.args.get("callsign", ""),
        recent_cards=recent_cards,
    )


@app.route("/admin/photomap/manage")
@admin_required
def admin_photomap_manage():
    """Every uploaded card, callsign by callsign, with Edit/Delete
    actions -- for fixing entries that went up without a photo or
    without QSO info (both are optional at upload time; this is where
    that gets cleaned up after the fact)."""
    try:
        cards = photomap_store.list_all_photo_cards()
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        cards = []
    return render_template("admin_photomap_manage.html", cards=cards)


@app.route("/admin/photomap/edit/<int:card_id>", methods=["GET", "POST"])
@admin_required
def admin_photomap_edit(card_id):
    try:
        card = photomap_store.get_photo_card(card_id)
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        return redirect(url_for("admin_photomap_manage"))

    if card is None:
        flash("That card no longer exists.", "error")
        return redirect(url_for("admin_photomap_manage"))

    if request.method == "POST":
        qso_date = request.form.get("qso_date", "").strip()
        band = request.form.get("band", "").strip().upper()
        mode = request.form.get("mode", "").strip().upper()
        freq = request.form.get("freq", "").strip()
        rst_sent = request.form.get("rst_sent", "").strip()
        rst_rcvd = request.form.get("rst_rcvd", "").strip()
        note = request.form.get("note", "").strip()
        try:
            photomap_store.update_photo_card(card_id, qso_date, band, mode, freq, rst_sent, rst_rcvd, note)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(url_for("admin_photomap_edit", card_id=card_id))

        removed = 0
        for s3_key in request.form.getlist("remove_image"):
            try:
                photomap_store.remove_photo_card_image(card_id, s3_key)
                delete_object(s3_key)
                removed += 1
            except S3Error:
                # The card's metadata is already updated even if the S3
                # delete itself fails -- a leftover orphaned object in
                # the bucket isn't worth blocking the save over.
                pass

        added, failed = 0, 0
        for f in request.files.getlist("images"):
            if not f or not f.filename:
                continue
            try:
                key = upload_card_image(f, card["callsign"])
                photomap_store.add_photo_card_image(card_id, key)
                added += 1
            except S3Error:
                failed += 1

        message = f"Updated {card['callsign']}."
        if added:
            message += f" Added {added} photo(s)."
        if removed:
            message += f" Removed {removed} photo(s)."
        if failed:
            message += f" {failed} new image(s) failed to upload."
        flash(message, "success")
        return redirect(url_for("admin_photomap_edit", card_id=card_id))

    try:
        images = photomap_store.get_images_for_card(card_id)
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        images = []
    image_entries = [
        {"s3_key": img["s3_key"], "url": url_for("photomap_image", key=img["s3_key"])} for img in images
    ]
    return render_template("admin_photomap_edit.html", card=card, images=image_entries)


@app.route("/admin/photomap/delete/<int:card_id>", methods=["POST"])
@admin_required
def admin_photomap_delete(card_id):
    try:
        removed = photomap_store.delete_photo_card(card_id)
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        return redirect(url_for("admin_photomap_manage"))

    if removed is None:
        flash("That card was already gone.", "error")
        return redirect(url_for("admin_photomap_manage"))

    for s3_key in removed.get("images", []):
        try:
            delete_object(s3_key)
        except S3Error:
            pass  # metadata's already gone; a leftover S3 object isn't worth blocking on

    flash(f"Deleted {removed['callsign']}'s card.", "success")
    return redirect(url_for("admin_photomap_manage"))


@app.route("/photomap")
def photomap():
    return render_template("photomap.html")


@app.route("/photomap/image/<path:key>")
def photomap_image(key):
    """Serves a QSL card photo's bytes straight from S3 through Flask,
    instead of redirecting the browser to a presigned S3 URL -- see
    get_object_bytes() in s3.py for why. Restricted to the photocards/
    prefix: this route takes a raw S3 key from the URL path, so it's
    deliberately not a general "fetch any object" proxy."""
    if not key.startswith("photocards/"):
        abort(404)
    try:
        body, content_type = get_object_bytes(key)
    except S3Error as exc:
        logger.warning("QSL Photo Map S3 error: %s", exc)
        abort(404)
    return Response(
        body,
        mimetype=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.route("/photomap/api/pins")
def photomap_api_pins():
    try:
        points = photomap_store.list_map_points()
    except S3Error as exc:
        logger.warning("QSL Photo Map S3 error: %s", exc)
        return {"pins": [], "error": str(exc)}
    return {
        "pins": [
            {
                "callsign": p["callsign"],
                "lat": p["lat"],
                "lon": p["lon"],
                "country": p["country"],
                "state": p["state"],
                "card_count": p["card_count"],
            }
            for p in points
        ]
    }


@app.route("/photomap/api/callsign")
def photomap_api_callsign():
    # Callsign comes in as a query param, not a URL path segment --
    # callsigns like FP/KJ1V contain a "/", and a "/" inside a path
    # segment gets split into two segments (or mangled by %2F-decoding
    # upstream of Flask) before routing ever sees it. A query string
    # doesn't have that problem.
    callsign = request.args.get("callsign", "").strip().upper()
    if not callsign:
        return {"callsign": "", "country": None, "state": None, "cards": [], "error": "Missing callsign."}, 400
    try:
        location = photomap_store.get_callsign_location(callsign)
        cards = photomap_store.get_cards_for_callsign(callsign)
    except S3Error as exc:
        logger.warning("QSL Photo Map S3 error: %s", exc)
        return {"callsign": callsign, "country": None, "state": None, "cards": [], "error": str(exc)}

    result_cards = []
    for card in cards:
        try:
            images = photomap_store.get_images_for_card(card["id"])
        except S3Error as exc:
            logger.warning("QSL Photo Map S3 error: %s", exc)
            images = []
        urls = [url_for("photomap_image", key=img["s3_key"]) for img in images]
        result_cards.append(
            {
                "qso_date": card["qso_date"],
                "band": card["band"],
                "mode": card["mode"],
                "freq": card["freq"],
                "rst_sent": card["rst_sent"],
                "rst_rcvd": card["rst_rcvd"],
                "note": card["note"],
                "images": urls,
            }
        )

    return {
        "callsign": callsign,
        "country": location["country"] if location else None,
        "state": location["state"] if location else None,
        "cards": result_cards,
    }


if __name__ == "__main__":
    app.run(debug=True)
