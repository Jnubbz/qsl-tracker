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
        lines = [self.name, self.address]
        city_line = ", ".join(p for p in (self.city, self.state) if p)
        if self.zip_code:
            city_line = f"{city_line} {self.zip_code}".strip()
        if city_line:
            lines.append(city_line)
        if self.country:
            lines.append(self.country)
        return "\n".join(line for line in lines if line)


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

    mqsl = _text(callsign_el, "mqsl") == "1"
    eqsl = _text(callsign_el, "eqsl") == "1"
    lotw = _text(callsign_el, "lotw") == "1"
    qslmgr = _text(callsign_el, "qslmgr")

    # QRZ's XML API doesn't expose a single "accepts Direct" boolean.
    # `mqsl` means "will return a paper card" and an empty `qslmgr`
    # means cards go straight to the operator rather than a manager --
    # together that's the closest signal to "wants direct QSL cards".
    # Adjust this rule if you find a better proxy in practice.
    accepts_direct = mqsl and not qslmgr

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
