"""
QSL Tracker -- look up callsigns against QRZ and keep track of which
ones want a direct paper QSL card.

No accounts: each visitor gets an anonymous session id (stored in a
signed cookie) that scopes their results in SQLite. Visitors log in
with their own QRZ XML subscriber credentials; we exchange those for a
short-lived QRZ session key and never store the password.
"""
from __future__ import annotations

import os
import secrets

from flask import Flask, flash, redirect, render_template, request, session, url_for

import db
from adif import distinct_callsigns, parse_adif
from qrz import QrzError, get_session_key, lookup_callsign

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
db.init_db()

# Cap how many callsigns we'll look up from a single ADIF upload in one
# request, so one big log file can't tie up the server or hammer QRZ.
MAX_LOOKUPS_PER_UPLOAD = 200


@app.before_request
def ensure_session():
    if "session_id" not in session:
        session["session_id"] = secrets.token_urlsafe(24)
    db.purge_expired()


def qrz_key_or_none():
    return session.get("qrz_key")


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

    # Only the short-lived session key is kept -- never the password.
    session["qrz_key"] = key
    session["qrz_username"] = username
    flash("Logged in to QRZ.", "success")
    return redirect(url_for("dashboard"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("qrz_key", None)
    session.pop("qrz_username", None)
    return redirect(url_for("index"))


@app.route("/dashboard")
def dashboard():
    if not qrz_key_or_none():
        flash("Log in with your QRZ credentials first.", "error")
        return redirect(url_for("index"))

    direct_only = request.args.get("filter") == "direct"
    contacts = db.get_contacts(session["session_id"], direct_only=direct_only)
    return render_template(
        "dashboard.html",
        contacts=contacts,
        direct_only=direct_only,
        qrz_username=session.get("qrz_username"),
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
    for callsign in callsigns:
        try:
            record = lookup_callsign(key, callsign)
            db.upsert_contact(session["session_id"], record)
            looked_up += 1
        except QrzError:
            failed += 1
        except Exception:
            failed += 1

    message = f"Looked up {looked_up} of {len(callsigns)} callsigns from your log."
    if failed:
        message += f" ({failed} failed or had no QRZ record.)"
    flash(message, "success" if looked_up else "error")
    return redirect(url_for("dashboard"))


@app.route("/clear", methods=["POST"])
def clear():
    db.clear_session(session["session_id"])
    flash("Cleared your results.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(debug=True)
