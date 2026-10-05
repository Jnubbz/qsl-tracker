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

import logging
import os
import secrets
import time
from datetime import date, datetime, timedelta
from functools import wraps

from flask import Flask, Response, abort, flash, redirect, render_template, request, session, url_for

import db
import labels
import photomap_store
from adif import distinct_callsigns, parse_adif
from mailer import send_qsl_request_email
from qrz import (
    LOG_PAGE_SIZE,
    QrzError,
    fetch_log_page,
    fetch_status,
    get_session_key,
    lookup_callsign_raw,
    lookup_location,
    status_total_qsos,
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

# Cap how many callsigns the Raw Address Ripper's ADIF bulk-add will
# look up in one request, so one big log file can't tie up the server or hammer QRZ.
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

# The QRZ Logbook API's own per-logbook access key (QRZ.com -> Logbook
# -> Settings -> API) -- a different secret from the QRZ username/
# password the logins use. Drives the "Sync from QRZ" button on the log
# page (admin_log() / admin_log_sync() below), which mirrors the whole
# logbook into photomap_store by AFTERLOGID paging -- see qrz.py's
# fetch_log_page() for why this replaced the old per-callsign CALL: fetch.
QRZ_LOGBOOK_API_KEY = os.environ.get("QRZ_LOGBOOK_API_KEY", "")

# A sync pages through the logbook synchronously, inside the request.
# Gunicorn kills a worker after 30s by default, so stop starting new
# pages after this many seconds (one page can still take up to the
# request timeout in qrz.fetch_log_page()); the cursor is saved after
# every page, so clicking Sync again just carries on from there.
QRZ_SYNC_TIME_BUDGET_SECONDS = 12

# After an incremental sync catches up, re-read this many recent days by
# date too -- see admin_log_sync().
QRZ_SYNC_RECENT_DAYS = 7

# Big logs make an unfiltered QSO Labels table enormous -- show this
# many most-recent rows and ask for a filter to see further back.
QSO_TABLE_ROW_CAP = 200


@app.template_filter("utc_time")
def utc_time(ts) -> str:
    """Unix timestamp -> "2026-10-04 21:08 UTC" for the log/sync status lines."""
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except (TypeError, ValueError):
        return ""


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
    if qrz_key_or_none():
        return redirect(url_for("raw_address"))
    return render_template("index.html")


@app.route("/login", methods=["POST"])
def login():
    key = qrz_login_attempt(
        request.form.get("qrz_username", ""), request.form.get("qrz_password", "")
    )
    if key is None:
        return redirect(url_for("index"))
    flash("Logged in to QRZ.", "success")
    return redirect(url_for("raw_address"))


@app.route("/logout", methods=["POST"])
def logout():
    db.clear_auth(session["session_id"])
    return redirect(url_for("index"))


# Retired 2026-10-04: the old Dashboard (direct-only lookup + CSV export)
# was folded into the Raw Address Ripper, which shows every address and
# flags the ones not marked for a direct card. Old bookmarks still land
# somewhere useful.
@app.route("/dashboard")
@app.route("/export.csv")
def dashboard():
    return redirect(url_for("raw_address"))


@app.route("/raw-address")
def raw_address():
    """Raw Address Ripper -- the app's one mailing-address tool (since
    2026-10-04, when it replaced the old Dashboard and both admin
    address pages). Any visitor logged in with their own QRZ XML
    subscriber account looks up one callsign or bulk-uploads an ADIF log
    (raw_address_adif_batch() below) and gets whatever mailing address
    QRZ has on file, with a warning when the station isn't marked as
    accepting a direct card. Josh uses it too -- admin login already
    requires a QRZ session.

    **Nothing here is ever written to SQLite or S3.** The batch lives
    only in this visitor's own signed session cookie as minimal
    {callsign, name, position} entries, re-fetched fresh from QRZ at
    print time, never an address snapshot."""
    if not qrz_key_or_none():
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    callsign = request.args.get("callsign", "").strip().upper()
    position = labels.clamp_mailing_position(_parse_int(request.args.get("position"), default=1))
    record = None
    error = None

    if callsign:
        key = qrz_key_or_none()
        try:
            record = lookup_callsign_raw(key, callsign)
            if not record.address:
                error = f"QRZ has no address on file at all for {record.callsign}."
                record = None
        except QrzError as exc:
            error = f"QRZ lookup failed: {exc}"
        except Exception:
            error = "Couldn't reach QRZ right now. Try again in a moment."

    batch = session.get("public_raw_batch", [])
    batch_used_positions = {b["position"] for b in batch}
    auth = current_auth()
    return render_template(
        "raw_address.html",
        callsign=callsign,
        position=position,
        label_count=labels.MAILING_LABEL_COUNT,
        record=record,
        label_lines=labels.label_lines(record) if record else [],
        error=error,
        batch=batch,
        batch_full=len(batch) >= labels.MAILING_LABEL_COUNT,
        next_batch_position=_next_free_position(batch_used_positions, labels.MAILING_LABEL_COUNT),
        adif_upload_cap=MAX_LOOKUPS_PER_UPLOAD,
        qrz_username=auth["qrz_username"] if auth else None,
    )


@app.route("/raw-address/pdf")
def raw_address_pdf():
    callsign = request.args.get("callsign", "").strip().upper()
    position = labels.clamp_mailing_position(_parse_int(request.args.get("position"), default=1))
    if not callsign:
        abort(400)

    key = qrz_key_or_none()
    if not key:
        abort(401)

    try:
        record = lookup_callsign_raw(key, callsign)
    except QrzError:
        abort(404)
    except Exception:
        abort(502)

    if not record.address:
        abort(404)

    pdf_bytes = labels.generate_mailing_label_pdf(record, position)
    filename = f"qsl-raw-address-{record.callsign}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/raw-address/batch/add", methods=["POST"])
def raw_address_batch_add():
    """Add one address to the visitor's own raw-address batch (see
    raw_address() above) at a chosen position, or the next free one if
    no position is sent (the QSO Labels page's per-row "Address"
    button). Stored under this visitor's own `public_raw_batch`."""
    callsign = request.form.get("callsign", "").strip().upper()
    if request.form.get("position"):
        position = labels.clamp_mailing_position(_parse_int(request.form.get("position"), default=1))
    else:
        # No position sent (e.g. the "Address" button on QSO Labels) --
        # take the next free one, same as the ADIF bulk-add does.
        used = {b["position"] for b in session.get("public_raw_batch", [])}
        position = _next_free_position(used, labels.MAILING_LABEL_COUNT) or 1
    redirect_to = _safe_next(url_for("raw_address", callsign=callsign, position=position))

    key = qrz_key_or_none()
    if not key:
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    if not callsign:
        flash("Enter a callsign to add.", "error")
        return redirect(redirect_to)

    try:
        record = lookup_callsign_raw(key, callsign)
    except QrzError as exc:
        flash(f"QRZ lookup failed: {exc}", "error")
        return redirect(redirect_to)
    except Exception:
        flash("Couldn't reach QRZ right now. Try again in a moment.", "error")
        return redirect(redirect_to)

    if not record.address:
        flash(f"QRZ has no address on file at all for {record.callsign} -- not added.", "error")
        return redirect(redirect_to)

    batch = session.get("public_raw_batch", [])
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
        session["public_raw_batch"] = batch
        flash(f"Added {record.callsign} at position {position}.", "success")

    return redirect(redirect_to)


@app.route("/raw-address/batch/remove", methods=["POST"])
def raw_address_batch_remove():
    position = _parse_int(request.form.get("position"), default=0)
    batch = session.get("public_raw_batch", [])
    session["public_raw_batch"] = [b for b in batch if b["position"] != position]
    return redirect(_safe_next(url_for("raw_address")))


@app.route("/raw-address/batch/clear", methods=["POST"])
def raw_address_batch_clear():
    session.pop("public_raw_batch", None)
    return redirect(_safe_next(url_for("raw_address")))


@app.route("/raw-address/batch/pdf")
def raw_address_batch_pdf():
    """One combined PDF with every address currently in this visitor's
    own raw-address batch, re-fetching each entry fresh from QRZ at print time rather than
    printing a stale snapshot."""
    batch = session.get("public_raw_batch", [])
    if not batch:
        abort(400)

    key = qrz_key_or_none()
    if not key:
        abort(401)

    items = []
    dropped = []
    for entry in batch:
        try:
            record = lookup_callsign_raw(key, entry["callsign"])
        except Exception:
            dropped.append(entry["callsign"])
            continue
        if not record.address:
            dropped.append(entry["callsign"])
            continue
        items.append((record, entry["position"]))

    if not items:
        abort(404)

    if dropped:
        flash(
            "Skipped in this print (QRZ has no address on file at all): "
            + ", ".join(dropped),
            "error",
        )

    pdf_bytes = labels.generate_mailing_batch_pdf(items)
    filename = f"qsl-raw-addresses-batch-{date.today().isoformat()}.pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.route("/raw-address/adif-batch", methods=["POST"])
def raw_address_adif_batch():
    """Public ADIF bulk-upload for Raw Address Ripper: any logged-in
    visitor uploads their own log and every distinct callsign in it
    (capped at MAX_LOOKUPS_PER_UPLOAD) gets a raw QRZ lookup, added
    straight to their own batch. Paced and circuit-broken
    (LOOKUP_DELAY_SECONDS between requests so a big log doesn't hammer
    QRZ, UPLOAD_TIME_BUDGET_SECONDS wall-clock cutoff, and
    MAX_CONSECUTIVE_FAILURES to bail out of an apparent QRZ outage early)
    since this runs on a real public request, not Josh's own trusted
    admin session. Stops adding once the sheet is full (30 positions)
    without even calling QRZ for anything past that point.

    Same storage guarantee as the rest of this feature: the uploaded
    file and every parsed QSO exist only for this one request and are
    never written anywhere -- only the minimal batch entries land in
    the visitor's own session."""
    redirect_to = _safe_next(url_for("raw_address"))

    key = qrz_key_or_none()
    if not key:
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    file_storage = request.files.get("adif_file")
    if not file_storage or not file_storage.filename:
        flash("Choose an ADIF (.adi/.adif) file to upload.", "error")
        return redirect(redirect_to)

    try:
        text = file_storage.read().decode("utf-8", errors="replace")
    except Exception:
        flash("Couldn't read that file -- is it a text ADIF export?", "error")
        return redirect(redirect_to)

    callsigns = distinct_callsigns(parse_adif(text))[:MAX_LOOKUPS_PER_UPLOAD]

    if not callsigns:
        flash(f"No callsigns found in {file_storage.filename} -- is it a valid ADIF log?", "error")
        return redirect(redirect_to)

    batch = session.get("public_raw_batch", [])
    used_positions = {b["position"] for b in batch}
    already_batched = {b["callsign"] for b in batch}

    added, no_address, lookup_failed, duplicates, sheet_full = [], [], [], [], []
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

        if callsign in already_batched:
            duplicates.append(callsign)
        else:
            position = _next_free_position(used_positions, labels.MAILING_LABEL_COUNT)
            if position is None:
                sheet_full.append(callsign)
            else:
                try:
                    record = lookup_callsign_raw(key, callsign)
                except QrzError:
                    lookup_failed.append(callsign)
                    consecutive_failures += 1
                except Exception:
                    lookup_failed.append(callsign)
                    consecutive_failures += 1
                else:
                    consecutive_failures = 0
                    if not record.address:
                        no_address.append(callsign)
                    else:
                        batch.append({"callsign": record.callsign, "name": record.name, "position": position})
                        used_positions.add(position)
                        already_batched.add(record.callsign)
                        added.append(record.callsign)

        if i < len(callsigns) - 1:
            time.sleep(LOOKUP_DELAY_SECONDS)

    session["public_raw_batch"] = batch

    summary = [f"{file_storage.filename}: {len(callsigns)} distinct callsign(s) found."]
    if added:
        summary.append(f"Added {len(added)} to the batch: {', '.join(added)}.")
    if no_address:
        summary.append(f"No address on file for {len(no_address)}: {', '.join(no_address)}.")
    if lookup_failed:
        summary.append(f"QRZ lookup failed for {len(lookup_failed)}: {', '.join(lookup_failed)}.")
    if duplicates:
        summary.append(f"Already in the batch, skipped: {', '.join(duplicates)}.")
    if sheet_full:
        summary.append(
            f"Sheet is full ({labels.MAILING_LABEL_COUNT} of {labels.MAILING_LABEL_COUNT}), "
            f"not added: {', '.join(sheet_full)}."
        )
    if stopped_early:
        remaining = len(callsigns) - (len(added) + len(no_address) + len(lookup_failed) + len(duplicates) + len(sheet_full))
        summary.append(
            f"Stopped early ({stopped_early}) -- {remaining} callsign(s) not attempted. "
            "Upload the log again to pick up more (already-added ones will just show as duplicates)."
        )

    flash(" ".join(summary), "success" if added else "error")
    return redirect(redirect_to)


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
    next_url = request.values.get("next") or url_for("admin_qso_label")

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


# Retired 2026-10-04: "Print Mailing Label" and the admin Raw Address
# Ripper were both superseded by the public Raw Address Ripper (admin
# login already requires a QRZ session, so it works as-is for Josh).
@app.route("/admin/label")
@app.route("/admin/raw-address")
def admin_label_retired():
    return redirect(url_for("raw_address"))


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


def _safe_next(fallback: str) -> str:
    """Where a batch add/remove/clear form should redirect back to.
    Every such form's default target is its own page (`fallback`,
    already a same-app url_for(...) result) -- but the browse-by-DXCC
    page (admin_qsos()) also points these same forms at itself via a
    hidden `next` field, so adding/removing from there doesn't bounce
    Josh away to the single-lookup page. Only ever trusts a `next` that
    is a path on this app (starts with exactly one leading "/", never
    "//" which browsers treat as protocol-relative -- i.e. off-site);
    anything else is quietly ignored in favor of the normal fallback,
    same as if `next` had never been sent."""
    target = request.form.get("next", "")
    if target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


@app.route("/admin/qso-label")
@admin_required
def admin_qso_label():
    """QSO Labels -- the one page for printing labels of Josh's own
    logged QSOs (callsign, UTC date/time, and a details grid of
    band/mode/freq/grid/RST, on an Avery 5163/8163 2"x4" sheet).

    Merged 2026-10-04 from the old single-callsign "Print QSO Label"
    page and the "Browse by DXCC Entity" list: one table of the log,
    filterable by callsign (substring) and/or DXCC entity, with
    checkbox multi-select into the batch, a per-row "Preview" (which
    also allows a hand-picked sheet position), and a per-row "Address"
    button that drops that station into the Raw Address Ripper batch.

    The log itself comes from photomap_store -- filled by "Sync from
    QRZ" or an ADIF upload on the log page (admin_log())."""
    callsign = request.args.get("callsign", "").strip().upper()
    entity = request.args.get("entity", "")
    qso_key = request.args.get("qso_key", "")
    position = labels.clamp_qso_position(_parse_int(request.args.get("position"), default=1))

    qso = None
    rows = []
    entities = []
    total = 0
    sync_state = {}
    try:
        entities = photomap_store.list_dxcc_entities()
        rows = photomap_store.list_all_my_qsos(dxcc_entity=entity, callsign=callsign)
        total = photomap_store.count_my_qsos()
        sync_state = photomap_store.qrz_sync_state()
        if qso_key:
            qso = photomap_store.get_my_qso(_parse_int(qso_key, default=0))
            if qso is None:
                flash("That logged QSO couldn't be found -- pick it again.", "error")
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")

    shown_rows = rows[:QSO_TABLE_ROW_CAP]
    self_url = url_for(
        "admin_qso_label", callsign=callsign or None, entity=entity or None
    )

    batch = session.get("qso_batch", [])
    batch_used_positions = {b["position"] for b in batch}
    address_batch = session.get("public_raw_batch", [])
    return render_template(
        "admin_qso_label.html",
        callsign=callsign,
        entity=entity,
        entities=entities,
        rows=shown_rows,
        row_count=len(rows),
        row_cap=QSO_TABLE_ROW_CAP,
        total_qsos=total,
        sync_state=sync_state,
        logbook_configured=bool(QRZ_LOGBOOK_API_KEY),
        self_url=self_url,
        position=position,
        label_count=labels.QSO_LABEL_COUNT,
        qso=qso,
        qso_fields=labels.qso_label_fields(qso) if qso else None,
        batch=batch,
        batch_full=len(batch) >= labels.QSO_LABEL_COUNT,
        next_batch_position=_next_free_position(batch_used_positions, labels.QSO_LABEL_COUNT),
        address_batch_count=len(address_batch),
        mailing_label_count=labels.MAILING_LABEL_COUNT,
        has_qrz_session=bool(qrz_key_or_none()),
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
    redirect_to = _safe_next(url_for("admin_qso_label", callsign=callsign))

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


@app.route("/admin/qso-label/batch/add-selected", methods=["POST"])
@admin_required
def admin_qso_label_batch_add_selected():
    """Add every checked QSO from the matches table on admin_qso_label()
    to the running batch in one action, instead of picking a position
    and submitting one at a time for each -- the common case when one
    callsign has several logged QSOs and Josh wants more than one of
    them on the sheet. Positions are auto-assigned in order via
    _next_free_position(); for manual control over which exact
    position one QSO lands on, click "Select" to preview it and add it
    from there instead (unchanged).

    All-or-nothing: if there isn't room for every newly-checked (not
    already-batched) QSO, nothing is added, so a partial batch never
    gets built by surprise."""
    raw_ids = request.form.getlist("qso_id")
    callsign = request.form.get("callsign", "").strip().upper()
    redirect_to = _safe_next(url_for("admin_qso_label", callsign=callsign))

    if not raw_ids:
        flash("Check at least one QSO to add, then try again.", "error")
        return redirect(redirect_to)

    batch = session.get("qso_batch", [])
    used_positions = {b["position"] for b in batch}
    already_batched_ids = {b["qso_id"] for b in batch}

    seen = set()
    qso_ids = []
    for raw in raw_ids:
        qso_id = _parse_int(raw, default=0)
        if qso_id and qso_id not in seen:
            seen.add(qso_id)
            qso_ids.append(qso_id)

    to_add = []  # list of (qso_id, qso dict)
    skipped_already_batched = 0
    not_found = 0
    for qso_id in qso_ids:
        if qso_id in already_batched_ids:
            skipped_already_batched += 1
            continue
        try:
            qso = photomap_store.get_my_qso(qso_id)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(redirect_to)
        if qso is None:
            not_found += 1
            continue
        to_add.append((qso_id, qso))

    if not to_add:
        if skipped_already_batched and not not_found:
            flash("Every QSO you checked is already in the batch.", "error")
        else:
            flash("Couldn't find any of the checked QSOs -- try again.", "error")
        return redirect(redirect_to)

    free_slots = labels.QSO_LABEL_COUNT - len(batch)
    if len(to_add) > free_slots:
        flash(
            f"You checked {len(to_add)} new QSO{'s' if len(to_add) != 1 else ''}, "
            f"but only {free_slots} position{'s' if free_slots != 1 else ''} "
            f"{'are' if free_slots != 1 else 'is'} free on this sheet -- remove "
            "some from the batch, or check fewer, and try again. Nothing was added.",
            "error",
        )
        return redirect(redirect_to)

    for qso_id, qso in to_add:
        position = _next_free_position(used_positions, labels.QSO_LABEL_COUNT)
        used_positions.add(position)
        batch.append({
            "qso_id": qso_id,
            "position": position,
            "callsign": qso["callsign"],
            "qso_date": qso.get("qso_date", ""),
        })
    session["qso_batch"] = batch

    parts = [f"Added {len(to_add)} QSO{'s' if len(to_add) != 1 else ''} to the batch."]
    if skipped_already_batched:
        parts.append(f"{skipped_already_batched} already in the batch, skipped.")
    if not_found:
        parts.append(f"{not_found} couldn't be found, skipped.")
    flash(" ".join(parts), "success")

    return redirect(redirect_to)


@app.route("/admin/qso-label/batch/remove", methods=["POST"])
@admin_required
def admin_qso_label_batch_remove():
    position = _parse_int(request.form.get("position"), default=0)
    batch = session.get("qso_batch", [])
    session["qso_batch"] = [b for b in batch if b["position"] != position]
    return redirect(_safe_next(url_for("admin_qso_label")))


@app.route("/admin/qso-label/batch/clear", methods=["POST"])
@admin_required
def admin_qso_label_batch_clear():
    session.pop("qso_batch", None)
    return redirect(_safe_next(url_for("admin_qso_label")))


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


# Merged into admin_qso_label() above on 2026-10-04 -- keep the old
# browse-by-DXCC URL (filters included) working.
@app.route("/admin/qsos")
@admin_required
def admin_qsos():
    return redirect(url_for(
        "admin_qso_label",
        entity=request.args.get("entity") or None,
        callsign=request.args.get("callsign") or None,
    ))


@app.route("/admin/log", methods=["GET", "POST"])
@app.route("/admin/photomap/import-adif", methods=["GET", "POST"])
@admin_required
def admin_log():
    """Josh's own log, as the app sees it -- the data behind QSO Labels
    and the card-upload page's QSO auto-fill. Two ways to fill it, both
    landing in the same photomap_store rows (deduplicated, so using both
    is safe): "Sync from QRZ" (admin_log_sync() below) or uploading an
    ADIF export here (POST)."""
    if request.method == "POST":
        file = request.files.get("adif_file")
        if not file or not file.filename:
            flash("Choose an ADIF (.adi) file to upload.", "error")
            return redirect(url_for("admin_log"))
        try:
            text = file.read().decode("utf-8", errors="ignore")
        except Exception:
            flash("Couldn't read that file.", "error")
            return redirect(url_for("admin_log"))

        qsos = parse_adif(text)
        try:
            added, backfilled = photomap_store.import_my_qsos(qsos)
        except S3Error as exc:
            flash(s3_config_hint(exc), "error")
            return redirect(url_for("admin_log"))
        message = f"Imported {added} new QSO record(s) ({len(qsos)} found in the file)."
        if backfilled:
            message += f" Filled in missing details on {backfilled} already-imported record(s)."
        flash(message, "success")
        return redirect(url_for("admin_log"))

    try:
        qso_count = photomap_store.count_my_qsos()
        sync_state = photomap_store.qrz_sync_state()
        newest = photomap_store.newest_qso_date()
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        qso_count, sync_state, newest = 0, {}, ""
    return render_template(
        "admin_log.html",
        qso_count=qso_count,
        sync_state=sync_state,
        logbook_configured=bool(QRZ_LOGBOOK_API_KEY),
        newest_qso_date=f"{newest[:4]}-{newest[4:6]}-{newest[6:]}" if newest else "",
    )


@app.route("/admin/log/sync", methods=["POST"])
@admin_required
def admin_log_sync():
    """Pull Josh's QRZ Logbook into the app's log store, 250 QSOs per
    request to QRZ, resuming from the saved AFTERLOGID cursor (so the
    first sync walks the whole logbook and later ones only fetch new
    QSOs). `full=1` restarts from the beginning -- useful once, to tag
    already-imported ADIF rows with their QRZ log ids, or if something
    looks off. Stops starting new pages after QRZ_SYNC_TIME_BUDGET_SECONDS
    and says so; the cursor is saved after every page, so pressing Sync
    again carries on exactly where it stopped."""
    redirect_to = _safe_next(url_for("admin_log"))
    if not QRZ_LOGBOOK_API_KEY:
        flash(
            "QRZ sync isn't set up yet: add QRZ_LOGBOOK_API_KEY in Render's "
            "Environment settings (QRZ.com -> Logbook -> Settings -> API key).",
            "error",
        )
        return redirect(redirect_to)

    try:
        state = photomap_store.qrz_sync_state()
    except S3Error as exc:
        flash(s3_config_hint(exc), "error")
        return redirect(redirect_to)

    full = request.form.get("full") == "1"
    since = until = None
    mode = "incremental"
    if full:
        cursor = 0
        mode = "full"
    elif not state.get("caught_up") and state.get("mode") == "full":
        # Carry on an unfinished "Re-sync everything" walk where it stopped.
        cursor = int(state.get("next_logid") or 0)
        mode = "full"
    elif state.get("caught_up"):
        # Normal incremental sync: only QSOs QRZ has added since last time.
        cursor = int(state.get("next_logid") or 0)
    else:
        # First sync (or an unfinished one): if the log already holds an
        # ADIF import, don't walk the whole QRZ logbook -- only ask for
        # QSOs dated from the newest one already here (minus a day of
        # overlap; duplicates are skipped anyway). Once this catches up,
        # later syncs switch to plain AFTERLOGID paging above.
        since = state.get("since")
        if not since:
            try:
                newest = photomap_store.newest_qso_date()
            except S3Error as exc:
                flash(s3_config_hint(exc), "error")
                return redirect(redirect_to)
            if newest:
                since = (datetime.strptime(newest, "%Y%m%d").date() - timedelta(days=1)).isoformat()
        until = (date.today() + timedelta(days=2)).isoformat() if since else None
        # A window that wasn't saved with the cursor means the cursor came
        # from an old whole-logbook walk -- restart paging inside the window.
        cursor = int(state.get("next_logid") or 0) if state.get("since") == since else 0
        mode = "window"
    started_at = time.monotonic()
    pages = fetched = added = backfilled = 0
    caught_up = False
    error = None

    while True:
        if pages and time.monotonic() - started_at > QRZ_SYNC_TIME_BUDGET_SECONDS:
            break
        try:
            adif_text = fetch_log_page(QRZ_LOGBOOK_API_KEY, cursor, since=since, until=until)
        except QrzError as exc:
            error = str(exc)
            break
        except Exception as exc:  # network trouble, timeouts, non-200s
            logger.warning("QRZ Logbook sync request failed: %s", exc)
            error = "Couldn't reach QRZ's Logbook API right now. Try again in a moment."
            break

        qsos = parse_adif(adif_text) if adif_text else []
        pages += 1
        if not qsos:
            caught_up = True
            break

        if since:
            # Guard: if QRZ ignored the date window, most of this page will
            # predate it -- stop instead of walking thousands of old QSOs.
            floor = since.replace("-", "")
            too_old = sum(1 for q in qsos if (q.fields.get("qso_date") or "99999999") < floor)
            if too_old > len(qsos) // 2:
                error = (
                    f"QRZ didn't apply the date filter (got QSOs older than {since}), "
                    "so the sync stopped rather than download your whole logbook."
                )
                break

        logids = [_parse_int(q.fields.get("app_qrzlog_logid"), default=0) for q in qsos]
        if not any(logids):
            error = "QRZ returned QSOs without log ids, so the sync can't page past them."
            break
        try:
            a, b = photomap_store.import_my_qsos(qsos)
        except S3Error as exc:
            error = s3_config_hint(exc)
            break
        added += a
        backfilled += b
        fetched += len(qsos)
        cursor = max(logids) + 1
        try:
            photomap_store.save_qrz_sync_state({
                "next_logid": cursor,
                "last_sync": time.time(),
                "caught_up": False,
                "since": since,
                "mode": mode,
            })
        except S3Error as exc:
            error = s3_config_hint(exc)
            break
        if len(qsos) < LOG_PAGE_SIZE:
            caught_up = True
            break

    # Safety net for an incremental sync: QRZ's docs never promise log
    # ids only increase, and a QSO with an id below the saved cursor
    # would be skipped forever. So once caught up, also re-read the last
    # few days by date (normally one small request; duplicates skipped).
    recent_added = 0
    if (caught_up and not error and mode == "incremental"
            and time.monotonic() - started_at < QRZ_SYNC_TIME_BUDGET_SECONDS + 6):
        recent_since = (date.today() - timedelta(days=QRZ_SYNC_RECENT_DAYS)).isoformat()
        recent_until = (date.today() + timedelta(days=2)).isoformat()
        recent_cursor = 0
        try:
            for _ in range(4):  # a few pages at most -- days, not the whole log
                adif_text = fetch_log_page(QRZ_LOGBOOK_API_KEY, recent_cursor,
                                           since=recent_since, until=recent_until)
                qsos = parse_adif(adif_text) if adif_text else []
                if not qsos:
                    break
                a_, b_ = photomap_store.import_my_qsos(qsos)
                recent_added += a_
                added += a_
                backfilled += b_
                fetched += len(qsos)
                ids = [_parse_int(q.fields.get("app_qrzlog_logid"), default=0) for q in qsos]
                if len(qsos) < LOG_PAGE_SIZE or not any(ids):
                    break
                recent_cursor = max(ids) + 1
        except (QrzError, S3Error) as exc:
            logger.warning("QRZ recent-days recheck failed: %s", exc)
        except Exception as exc:
            logger.warning("QRZ recent-days recheck failed: %s", exc)

    # QRZ's own count, so the page can say whether the two actually match.
    qrz_total = None
    if not error and time.monotonic() - started_at < QRZ_SYNC_TIME_BUDGET_SECONDS + 10:
        try:
            qrz_total = status_total_qsos(fetch_status(QRZ_LOGBOOK_API_KEY))
        except Exception as exc:
            logger.warning("QRZ Logbook STATUS failed: %s", exc)
    if qrz_total is not None:
        try:
            photomap_store.save_qrz_sync_state({"qrz_total": qrz_total, "qrz_total_at": time.time()})
        except S3Error:
            pass

    if caught_up:
        try:
            # A date-window sync that found nothing new has no log id to
            # resume from -- keep the window (cheap to re-ask) rather than
            # marking caught up at cursor 0, which would mean a whole-
            # logbook walk next time.
            no_anchor = since is not None and cursor == 0
            photomap_store.save_qrz_sync_state({
                "next_logid": cursor,
                "last_sync": time.time(),
                "caught_up": not no_anchor,
                "since": since if no_anchor else None,
                "mode": None,
            })
        except S3Error as exc:
            error = error or s3_config_hint(exc)

    parts = [f"Fetched {fetched} QSO(s) from QRZ: {added} new"]
    if backfilled:
        parts[0] += f", {backfilled} already-logged ones filled in"
    parts[0] += "."
    if recent_added:
        parts.append(f"({recent_added} of those turned up only in the recent-days recheck.)")
    if caught_up and not error:
        parts.append("Up to date with your QRZ Logbook.")
    elif not error:
        parts.append(
            "Stopped partway to stay under the server's time limit -- press Sync again to keep going"
            + (" (it picks up the full re-read where it left off)." if mode == "full" else ".")
        )
    if caught_up and not error and qrz_total is not None:
        try:
            here = photomap_store.count_my_qsos()
        except S3Error:
            here = None
        if here is not None and here < qrz_total:
            parts.append(
                f"QRZ has {qrz_total} QSOs, this log has {here} -- press "
                "\"Re-sync everything\" on the Your log page to pull in the rest."
            )
    if error:
        parts.append(error)
    flash(" ".join(parts), "error" if error else "success")
    return redirect(redirect_to)


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
