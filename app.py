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
    format_mailing_label,
    get_session_key,
    lookup_callsign,
    lookup_location,
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
# CURRENTLY UNUSED (2026-08-24): the QSO-label page briefly fetched
# live from this API (see qrz.py's fetch_logged_qsos()/fetch_recent_qsos()/
# fetch_raw(), all still present and untouched), but QRZ's own CALL:
# filter turned out to be broken account-wide that day (confirmed
# across multiple callsigns of different ages, with BETWEEN: still
# working fine) -- see qsl-tracker-status.md in the project docs for
# the full investigation. Reverted to the ADIF-imported log
# (photomap_store.find_my_qsos()/get_my_qso()) as the QSO-label data
# source in the meantime. This env var and qrz.py's Logbook API client
# are left in place, ready to be wired back into admin_qso_label()
# below whenever that's revisited -- nothing here needs to be rebuilt
# from scratch, just re-plugged in.
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
    Avery 8160/5160/5260 1"x2-5/8" address-label sheet.

    Also shows the address-label **batch** (see
    admin_label_batch_add()/etc. below): several different addresses,
    each at its own chosen position, so one sheet can print more than
    one card's worth of address without wasting the other 29 positions
    on a single lookup."""
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

    batch = session.get("address_batch", [])
    batch_used_positions = {b["position"] for b in batch}
    return render_template(
        "admin_label.html",
        callsign=callsign,
        position=position,
        label_count=labels.MAILING_LABEL_COUNT,
        record=record,
        label_lines=labels.label_lines(record) if record else [],
        error=error,
        batch=batch,
        batch_full=len(batch) >= labels.MAILING_LABEL_COUNT,
        next_batch_position=_next_free_position(batch_used_positions, labels.MAILING_LABEL_COUNT),
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


@app.route("/admin/label/batch/add", methods=["POST"])
@admin_required
def admin_label_batch_add():
    """Add one address label to the running batch (see admin_label()
    above) at a Josh-chosen position. Looks the callsign up fresh right
    now (same gating as the single-label flow) so a bad add fails
    immediately with a clear reason, rather than only surfacing at
    print time. Only `callsign`/`position`/`name` (for display) are
    kept in the session -- never the address itself -- so
    admin_label_batch_pdf() below always prints whatever QRZ says right
    now, not a stale snapshot from whenever this was added."""
    callsign = request.form.get("callsign", "").strip().upper()
    position = labels.clamp_mailing_position(_parse_int(request.form.get("position"), default=1))
    redirect_to = url_for("admin_label", callsign=callsign, position=position)

    if not callsign:
        flash("Enter a callsign to add.", "error")
        return redirect(redirect_to)

    key = qrz_key_or_none()
    if not key:
        return redirect(url_for("admin_login", next=redirect_to))

    try:
        record = lookup_callsign(key, callsign)
    except QrzError as exc:
        flash(f"QRZ lookup failed: {exc}", "error")
        return redirect(redirect_to)
    except Exception:
        flash("Couldn't reach QRZ right now. Try again in a moment.", "error")
        return redirect(redirect_to)

    if not record.accepts_direct:
        flash(
            f"No mailing address on file for {record.callsign} that they've made "
            "available for a direct card -- not added.",
            "error",
        )
        return redirect(redirect_to)

    batch = session.get("address_batch", [])
    if len(batch) >= labels.MAILING_LABEL_COUNT:
        flash(
            f"That sheet is full ({labels.MAILING_LABEL_COUNT} of "
            f"{labels.MAILING_LABEL_COUNT} positions used) -- remove one, or "
            "download/clear the batch first.",
            "error",
        )
    elif any(b["position"] == position for b in batch):
        flash(
            f"Position {position} is already used in this batch -- pick a "
            "different position, or remove that item first.",
            "error",
        )
    else:
        batch.append({"callsign": record.callsign, "name": record.name, "position": position})
        session["address_batch"] = batch
        flash(f"Added {record.callsign} at position {position}.", "success")

    return redirect(redirect_to)


@app.route("/admin/label/batch/remove", methods=["POST"])
@admin_required
def admin_label_batch_remove():
    position = _parse_int(request.form.get("position"), default=0)
    batch = session.get("address_batch", [])
    session["address_batch"] = [b for b in batch if b["position"] != position]
    return redirect(url_for("admin_label"))


@app.route("/admin/label/batch/clear", methods=["POST"])
@admin_required
def admin_label_batch_clear():
    session.pop("address_batch", None)
    return redirect(url_for("admin_label"))


@app.route("/admin/label/batch/pdf")
@admin_required
def admin_label_batch_pdf():
    """One combined PDF with every address currently in the batch, each
    at its own chosen position -- everything else on the sheet left
    blank, same as the single-label PDF. Re-looks-up each callsign
    fresh (the session only ever kept the callsign/position, never the
    address) so this always prints current QRZ data."""
    batch = session.get("address_batch", [])
    if not batch:
        abort(400)

    key = qrz_key_or_none()
    if not key:
        abort(401)

    items = []
    dropped = []
    for entry in batch:
        try:
            record = lookup_callsign(key, entry["callsign"])
        except Exception:
            dropped.append(entry["callsign"])
            continue
        if not record.accepts_direct:
            dropped.append(entry["callsign"])
            continue
        items.append((record, entry["position"]))

    if not items:
        abort(404)

    if dropped:
        flash(
            "Skipped in this print (no longer has a usable address on file): "
            + ", ".join(dropped),
            "error",
        )

    pdf_bytes = labels.generate_mailing_batch_pdf(items)
    filename = f"qsl-labels-batch-{date.today().isoformat()}.pdf"
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


def _next_free_position(used: set, count: int) -> int | None:
    """The lowest sheet position (1..count) not already in `used` --
    the default a batch "add" form pre-selects, so the common case
    (keep adding, let it fill in order) needs no extra clicks. Returns
    None if every position is already taken (the sheet is full)."""
    for position in range(1, count + 1):
        if position not in used:
            return position
    return None


@app.route("/admin/qso-label")
@admin_required
def admin_qso_label():
    """Print a label of one of Josh's own logged QSOs (callsign,
    date/time in UTC, band/mode/freq, RST sent/received) -- meant to be
    stuck onto a blank 2"x4" spot on the physical QSL card instead of
    hand-written. Reads from the imported ADIF log
    (photomap_store.find_my_qsos()/get_my_qso() -- upload/refresh it at
    /admin/photomap/import-adif), not a live QRZ fetch -- see the
    QRZ_LOGBOOK_API_KEY comment near the top of this file for why.

    A callsign can have several logged QSOs (repeat contacts, different
    bands/dates), so this is a two-step page: first pick a callsign and
    see the matches, then pick the specific QSO (`qso_key`, really the
    row's `id` as a string) to preview and print.

    Also shows the QSO-label **batch** (see
    admin_qso_label_batch_add()/etc. below): several different QSOs,
    each at its own chosen position, so one sheet can print more than
    one card's worth of labels without wasting the other 9 positions on
    a single QSO."""
    callsign = request.args.get("callsign", "").strip().upper()
    qso_key = request.args.get("qso_key", "")
    position = labels.clamp_qso_position(_parse_int(request.args.get("position"), default=1))

    matches = []
    qso = None
    error = None

    if callsign:
        try:
            matches = photomap_store.find_my_qsos(callsign)
        except S3Error as exc:
            error = s3_config_hint(exc)
            matches = []
        if not error and not matches:
            error = (
                f"No logged QSO with {callsign} found in your imported ADIF log. "
                "If you've logged this contact since your last import, upload an "
                "updated ADIF export first (see the link below the search box)."
            )
        elif not error and qso_key:
            qso = next((m for m in matches if str(m["id"]) == qso_key), None)
            if qso is None:
                error = "That logged QSO couldn't be found -- try picking it again."
            else:
                # A specific QSO was picked -- show its preview, not the
                # full match table again.
                matches = []

    batch = session.get("qso_batch", [])
    batch_used_positions = {b["position"] for b in batch}
    return render_template(
        "admin_qso_label.html",
        callsign=callsign,
        position=position,
        label_count=labels.QSO_LABEL_COUNT,
        matches=matches,
        qso=qso,
        qso_label_lines=labels.qso_label_lines(qso) if qso else [],
        error=error,
        batch=batch,
        batch_full=len(batch) >= labels.QSO_LABEL_COUNT,
        next_batch_position=_next_free_position(batch_used_positions, labels.QSO_LABEL_COUNT),
    )


@app.route("/admin/qso-label/pdf")
@admin_required
def admin_qso_label_pdf():
    qso_key = request.args.get("qso_key", "")
    position = labels.clamp_qso_position(_parse_int(request.args.get("position"), default=1))
    if not qso_key:
        abort(400)

    qso_id = _parse_int(qso_key, default=0)
    try:
        qso = photomap_store.get_my_qso(qso_id) if qso_id else None
    except S3Error:
        abort(502)
    if qso is None:
        abort(404)

    pdf_bytes = labels.generate_qso_label_pdf(qso, position)
    filename = f"qso-label-{qso['callsign']}-{qso.get('qso_date', '')}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/admin/qso-label/batch/add", methods=["POST"])
@admin_required
def admin_qso_label_batch_add():
    """Add one QSO label to the running batch (see admin_qso_label()
    above) at a Josh-chosen position. Only `qso_id`/`position` plus
    `callsign`/`qso_date` (for display) are kept in the session --
    admin_qso_label_batch_pdf() below re-reads the full record fresh
    from photomap_store at print time."""
    qso_id = _parse_int(request.form.get("qso_id"), default=0)
    position = labels.clamp_qso_position(_parse_int(request.form.get("position"), default=1))
    callsign = request.form.get("callsign", "").strip().upper()
    redirect_to = url_for("admin_qso_label", callsign=callsign)

    try:
        qso = photomap_store.get_my_qso(qso_id) if qso_id else None
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        return redirect(redirect_to)
    if qso is None:
        flash("Couldn't find that QSO to add -- try picking it again.", "error")
        return redirect(redirect_to)

    batch = session.get("qso_batch", [])
    if len(batch) >= labels.QSO_LABEL_COUNT:
        flash(
            f"That sheet is full ({labels.QSO_LABEL_COUNT} of "
            f"{labels.QSO_LABEL_COUNT} positions used) -- remove one, or "
            "download/clear the batch first.",
            "error",
        )
    elif any(b["position"] == position for b in batch):
        flash(
            f"Position {position} is already used in this batch -- pick a "
            "different position, or remove that item first.",
            "error",
        )
    else:
        batch.append({
            "qso_id": qso_id,
            "position": position,
            "callsign": qso["callsign"],
            "qso_date": qso.get("qso_date", ""),
        })
        session["qso_batch"] = batch
        flash(f"Added {qso['callsign']} ({qso.get('qso_date', '')}) at position {position}.", "success")

    return redirect(redirect_to)


@app.route("/admin/qso-label/batch/remove", methods=["POST"])
@admin_required
def admin_qso_label_batch_remove():
    position = _parse_int(request.form.get("position"), default=0)
    batch = session.get("qso_batch", [])
    session["qso_batch"] = [b for b in batch if b["position"] != position]
    return redirect(url_for("admin_qso_label"))


@app.route("/admin/qso-label/batch/clear", methods=["POST"])
@admin_required
def admin_qso_label_batch_clear():
    session.pop("qso_batch", None)
    return redirect(url_for("admin_qso_label"))


@app.route("/admin/qso-label/batch/pdf")
@admin_required
def admin_qso_label_batch_pdf():
    """One combined PDF with every QSO currently in the batch, each at
    its own chosen position -- everything else on the sheet left blank,
    same as the single-label PDF. Re-reads each QSO fresh from
    photomap_store (the session only ever kept the id/position)."""
    batch = session.get("qso_batch", [])
    if not batch:
        abort(400)

    items = []
    dropped = 0
    for entry in batch:
        try:
            qso = photomap_store.get_my_qso(entry["qso_id"])
        except S3Error:
            abort(502)
        if qso is None:
            dropped += 1
            continue
        items.append((qso, entry["position"]))

    if not items:
        abort(404)

    if dropped:
        flash(f"Skipped {dropped} item(s) in this print -- no longer found in your imported log.", "error")

    pdf_bytes = labels.generate_qso_batch_pdf(items)
    filename = f"qso-labels-batch-{date.today().isoformat()}.pdf"
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
