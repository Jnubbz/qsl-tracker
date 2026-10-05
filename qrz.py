"""
Minimal client for two separate QRZ.com APIs:

  * The **XML Logbook Data API** (`xmldata.qrz.com`) -- a paid ("XML"
    subscriber) feature. A visitor supplies their own QRZ
    username/password; we exchange those for a short-lived session key
    and never persist the password itself. Used for looking up a
    station's own profile (name/address/grid) -- `lookup_callsign_raw()`,
    `lookup_location()`.
    Docs: https://www.qrz.com/XML/current_spec.html

  * The **Logbook API** (`logbook.qrz.com/api`) -- a different QRZ
    product with its own authentication: a static per-logbook API key
    (from a QRZ account's Logbook -> Settings -> API page), not a
    username/password session. Used for mirroring every QSO in Josh's
    own QRZ Logbook into the app's log store -- `fetch_log_page()`. Requires the
    logbook owner's account to be at the XML subscriber level or
    higher (same tier the XML API above needs), but the two APIs don't
    share a session -- this one's key is a standing secret, not
    something a visitor logs in with.
    Docs: https://www.qrz.com/docs/logbook/QRZLogbookAPI.html
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
import html
from urllib.parse import unquote_plus

import requests

QRZ_XML_URL = "https://xmldata.qrz.com/xml/current/"
NS = {"qrz": "http://xmldata.qrz.com"}

QRZ_LOGBOOK_API_URL = "https://logbook.qrz.com/api"
# QRZ's own docs note generic user agents (e.g. the requests library's
# default) "may face rate limiting" -- an identifiable one avoids that.
QRZ_LOGBOOK_USER_AGENT = "qsl-tracker (https://github.com/Jnubbz/qsl-tracker)"


class QrzError(Exception):
    """Raised when QRZ rejects a login or a lookup."""


class QrzLogbookError(QrzError):
    """Raised when the QRZ Logbook API rejects a FETCH (bad/missing API
    key, or a non-OK RESULT)."""


def format_mailing_label(
    name: str, address: str, city: str, state: str, zip_code: str, country: str
) -> str:
    """Format a name + address into a ready-to-print mailing label block.

    One multi-line string: name, street address, "city, state zip", and
    country, each on their own line -- blank pieces just drop out rather
    than leaving an empty line. Shared by QrzRecord.full_address() and
    the dashboard's CSV export, so both produce identical label text.
    """
    lines = [name, address]
    city_line = ", ".join(p for p in (city, state) if p)
    if zip_code:
        city_line = f"{city_line} {zip_code}".strip()
    if city_line:
        lines.append(city_line)
    if country:
        lines.append(country)
    return "\n".join(line for line in lines if line)


@dataclass
class QrzRecord:
    callsign: str
    name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    country: str = ""
    grid: str = ""
    mqsl: bool = False       # will return a paper QSL (bureau or direct)
    eqsl: bool = False       # accepts eQSL
    lotw: bool = False       # uploads to Logbook of the World
    qsl_via: str = ""        # QSL manager / "via" note, if any
    accepts_direct: bool = False  # our derived "wants a direct card" flag

    def full_address(self) -> str:
        return format_mailing_label(
            self.name, self.address, self.city, self.state, self.zip_code, self.country
        )


@dataclass
class QrzLocation:
    """Just the location fields for a callsign -- country, state, county,
    grid square, and lat/lon -- for the QSL Photo Map. Deliberately a
    separate lookup from lookup_callsign() below: that function withholds
    country/state/address entirely unless accepts_direct is true, since
    it's built around "who do I mail a card to" and Josh already spent a
    long debugging session getting that gating right. The photo map only
    ever plots an approximate public location (QRZ's lat/lon for a
    callsign is typically grid-square-derived, not a street address), so
    it doesn't need or want that gating -- hence its own lookup instead
    of loosening the address-filter logic elsewhere."""
    callsign: str
    country: str = ""
    state: str = ""
    county: str = ""
    grid: str = ""
    lat: float | None = None
    lon: float | None = None


def _text(el, tag: str) -> str:
    node = el.find(f"qrz:{tag}", NS)
    return node.text.strip() if node is not None and node.text else ""


def get_session_key(username: str, password: str) -> str:
    """Log in to QRZ and return a session key, or raise QrzError."""
    resp = requests.get(
        QRZ_XML_URL,
        params={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)
    session = root.find("qrz:Session", NS)
    if session is None:
        raise QrzError("Unexpected response from QRZ.")

    error = _text(session, "Error")
    if error:
        raise QrzError(error)

    key = _text(session, "Key")
    if not key:
        raise QrzError("QRZ did not return a session key.")
    return key


def lookup_callsign_raw(session_key: str, callsign: str) -> QrzRecord:
    """Look up one callsign and return whatever mailing address QRZ has
    on file, full stop -- unlike lookup_callsign() above, which discards
    the address entirely unless accepts_direct is true.

    Built for "Raw Address Ripper": a contest exchange (or any other
    off-QRZ arrangement -- an on-air request, a club roster) can
    establish that a station wants a card mailed to them even though
    their QRZ page never says "direct" anywhere. An explicit opt-out
    (mqsl == "0"), a blank mqsl (the common case -- most operators never
    set it either way), or a QSL-manager note all hide the address on
    the regular lookup_callsign()/admin_label() flow -- correctly, for
    "who has volunteered a direct card" -- but none of them mean QRZ
    doesn't *have* an address on file for the operator. This returns
    that address whenever one exists, with no gating on any of it.

    Still computes `accepts_direct` the exact same way lookup_callsign()
    does, so a caller can show a "heads up, QRZ doesn't have this one
    marked as accepting direct" note without a second lookup -- it just
    no longer controls whether the address fields get populated.
    Raises QrzError for a QRZ-side error or an unknown callsign, same as
    lookup_callsign()."""
    resp = requests.get(
        QRZ_XML_URL,
        params={"s": session_key, "callsign": callsign},
        timeout=10,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    session = root.find("qrz:Session", NS)
    if session is not None:
        error = _text(session, "Error")
        if error:
            raise QrzError(error)

    callsign_el = root.find("qrz:Callsign", NS)
    if callsign_el is None:
        raise QrzError(f"No QRZ record found for {callsign}.")

    mqsl_raw = _text(callsign_el, "mqsl")
    mqsl = mqsl_raw == "1"
    eqsl = _text(callsign_el, "eqsl") == "1"
    lotw = _text(callsign_el, "lotw") == "1"
    qslmgr = _text(callsign_el, "qslmgr")
    has_address = bool(_text(callsign_el, "addr1"))

    # Same "is this actually a manager, or just free text that mentions
    # direct" logic as lookup_callsign() -- kept only to compute
    # accepts_direct below for display, never to gate the address itself.
    qslmgr_lower = qslmgr.lower()
    negates_direct = "no direct" in qslmgr_lower or "not direct" in qslmgr_lower
    mentions_direct = "direct" in qslmgr_lower and not negates_direct
    has_manager = bool(qslmgr) and not mentions_direct
    accepts_direct = has_address and mqsl_raw != "0" and not has_manager

    # The only gate here at all: QRZ actually has to have an address on
    # file. mqsl/has_manager don't touch these fields the way they do
    # in lookup_callsign() above.
    return QrzRecord(
        callsign=_text(callsign_el, "call") or callsign.upper(),
        name=" ".join(
            p for p in (_text(callsign_el, "fname"), _text(callsign_el, "name")) if p
        ),
        address=_text(callsign_el, "addr1"),
        city=_text(callsign_el, "addr2"),
        state=_text(callsign_el, "state"),
        zip_code=_text(callsign_el, "zip"),
        country=_text(callsign_el, "country"),
        grid=_text(callsign_el, "grid"),
        mqsl=mqsl,
        eqsl=eqsl,
        lotw=lotw,
        qsl_via=qslmgr,
        accepts_direct=accepts_direct,
    )


def lookup_location(session_key: str, callsign: str) -> QrzLocation:
    """Look up just country/state/county/grid/lat/lon for a callsign --
    see QrzLocation above for why this is separate from lookup_callsign()."""
    resp = requests.get(
        QRZ_XML_URL,
        params={"s": session_key, "callsign": callsign},
        timeout=10,
    )
    resp.raise_for_status()
    root = ET.fromstring(resp.text)

    session = root.find("qrz:Session", NS)
    if session is not None:
        error = _text(session, "Error")
        if error:
            raise QrzError(error)

    callsign_el = root.find("qrz:Callsign", NS)
    if callsign_el is None:
        raise QrzError(f"No QRZ record found for {callsign}.")

    def _float(tag: str) -> float | None:
        raw = _text(callsign_el, tag)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    return QrzLocation(
        callsign=_text(callsign_el, "call") or callsign.upper(),
        country=_text(callsign_el, "country"),
        state=_text(callsign_el, "state"),
        county=_text(callsign_el, "county"),
        grid=_text(callsign_el, "grid"),
        lat=_float("lat"),
        lon=_float("lon"),
    )


# ---------------------------------------------------------------------
# QRZ Logbook API -- full-log sync by AFTERLOGID paging (2026-10-04)
# ---------------------------------------------------------------------
#
# History: the first integration (Aug 2026) asked QRZ for one callsign at
# a time with OPTION=CALL:<call>, and QRZ's CALL: filter came back empty
# for every callsign tried while BETWEEN: worked -- so it was shelved.
# This version never uses CALL: at all. It pages through the *whole*
# logbook the way QRZ's own docs recommend ("MAX:250,AFTERLOGID:0", then
# AFTERLOGID = highest app_qrzlog_logid seen + 1, until a page comes back
# short) and lets the app do callsign matching locally, against its own
# copy of the log. New QSOs always get a higher logid, so a later sync
# that resumes from the saved cursor only pulls what's new.

LOG_PAGE_SIZE = 250


def _decode_adif_payload(adif_text: str) -> str:
    """QRZ returns the ADIF field inside a form-style RESULT=...&ADIF=...
    body, and depending on the endpoint/version its angle brackets have
    been seen HTML-entity-escaped (&lt;call:4&gt;) or percent-encoded
    (%3Ccall%3A4%3E) instead of raw. ADIF tag lengths count the *decoded*
    characters, so decode once, before parsing, and only when the raw
    form clearly isn't already there."""
    if "<" in adif_text:
        return adif_text
    if "&lt;" in adif_text.lower():
        return html.unescape(adif_text)
    if "%3c" in adif_text.lower():
        return unquote_plus(adif_text)
    return adif_text


def parse_logbook_response(text: str) -> tuple[dict, str]:
    """Split a Logbook API response into (header fields, decoded ADIF).

    The body is name=value pairs joined with "&" -- RESULT, COUNT,
    LOGIDS, ... and ADIF last -- but the ADIF value itself can contain a
    literal "&" (e.g. inside a COMMENT), so the header is everything
    before the "ADIF=" marker and the ADIF is everything after it, never
    a naive "&"-split of the whole body."""
    head, marker, adif_text = text.partition("ADIF=")
    fields = {}
    for pair in head.rstrip("&").split("&"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            fields[k.strip().upper()] = unquote_plus(v.strip())
    return fields, _decode_adif_payload(adif_text) if marker else ""


def fetch_log_page(api_key: str, after_logid: int, page_size: int = LOG_PAGE_SIZE,
                   timeout: float = 12) -> str:
    """One page of Josh's QRZ Logbook: up to `page_size` QSOs whose
    app_qrzlog_logid is >= `after_logid`, returned as decoded ADIF text
    (feed it to adif.parse_adif()). Returns "" when nothing is left.

    Raises QrzLogbookError for a rejected key or any other failure, with
    QRZ's own REASON text when it gives one."""
    resp = requests.post(
        QRZ_LOGBOOK_API_URL,
        data={
            "KEY": api_key,
            "ACTION": "FETCH",
            "OPTION": f"MAX:{page_size},AFTERLOGID:{after_logid}",
        },
        headers={"User-Agent": QRZ_LOGBOOK_USER_AGENT},
        timeout=timeout,
    )
    resp.raise_for_status()
    fields, adif_text = parse_logbook_response(resp.text)

    result = fields.get("RESULT", "")
    reason = fields.get("REASON", "")
    if result == "AUTH" or "invalid api key" in reason.lower() or "access denied" in reason.lower():
        raise QrzLogbookError(
            "QRZ rejected the Logbook API key -- check QRZ_LOGBOOK_API_KEY on Render "
            "(QRZ.com -> Logbook -> Settings -> API key)."
        )
    if fields.get("COUNT") == "0":
        return ""  # QRZ reports "nothing past this point" as COUNT=0, sometimes with RESULT=FAIL
    if result != "OK":
        raise QrzLogbookError(f"QRZ Logbook fetch failed: {reason or resp.text[:200] or 'empty response'}")
    return adif_text

