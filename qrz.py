"""
Minimal client for the QRZ.com XML Logbook Data API.

QRZ's XML API is a paid ("XML" subscriber) feature. A visitor supplies
their own QRZ username/password; we exchange those for a short-lived
session key and never persist the password itself.

Docs: https://www.qrz.com/XML/current_spec.html
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

QRZ_XML_URL = "https://xmldata.qrz.com/xml/current/"
NS = {"qrz": "http://xmldata.qrz.com"}


class QrzError(Exception):
    """Raised when QRZ rejects a login or a lookup."""


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


def lookup_callsign(session_key: str, callsign: str) -> QrzRecord:
    """Look up one callsign using an existing session key."""
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

    # QRZ's XML API doesn't expose a single "accepts Direct" boolean, and
    # `mqsl` per QRZ's own spec is "will return paper QSL (0/1 or blank
    # if unknown)" -- most operators simply never set it, so it's blank
    # far more often than it's an explicit "1". Requiring mqsl == "1"
    # threw away addresses for anyone who hadn't ticked that box, even
    # with a perfectly good mailing address on file.
    #
    # Instead: show the address whenever one exists and there's no
    # explicit signal against it -- an explicit opt-out (mqsl == "0")
    # or a QSL manager on file (cards should route through them, not
    # straight to the operator).
    # `qslmgr` is free text, and operators use it two different ways in
    # practice: naming an actual QSL manager to route cards through
    # ("via N0XYZ"), or describing which methods *they themselves*
    # accept ("Direct, LOTW, QRZ."). Only the first case should exclude
    # someone -- if the text itself says "direct" (and doesn't negate
    # it, e.g. "no direct"), that's actually a positive signal, not a
    # manager to route around.
    qslmgr_lower = qslmgr.lower()
    negates_direct = "no direct" in qslmgr_lower or "not direct" in qslmgr_lower
    mentions_direct = "direct" in qslmgr_lower and not negates_direct
    has_manager = bool(qslmgr) and not mentions_direct

    accepts_direct = has_address and mqsl_raw != "0" and not has_manager

    # We only keep a mailing address on file for operators who actually
    # want a direct card -- everyone else's address is simply discarded
    # here and never reaches the database.
    if accepts_direct:
        address = _text(callsign_el, "addr1")
        city = _text(callsign_el, "addr2")
        state = _text(callsign_el, "state")
        zip_code = _text(callsign_el, "zip")
        country = _text(callsign_el, "country")
    else:
        address = city = state = zip_code = country = ""

    return QrzRecord(
        callsign=_text(callsign_el, "call") or callsign.upper(),
        name=" ".join(
            p for p in (_text(callsign_el, "fname"), _text(callsign_el, "name")) if p
        ),
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
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
