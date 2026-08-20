"""
Tiny ADIF (.adi) parser.

We only need enough of the ADIF spec to pull distinct callsigns (plus a
little context per QSO) out of a log file -- not a full round-trip
parser/writer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A field tag is <name:length> or <name:length:type>. An end-of-record
# marker is just <eor> (or <EOR>), with no length. We match both and
# tell them apart by whether `length` was captured.
TAG_RE = re.compile(r"<(\w+)(?::(\d+)(?::\w+)?)?>", re.IGNORECASE)


@dataclass
class AdifQso:
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def callsign(self) -> str:
        return self.fields.get("call", "").upper()


def parse_adif(text: str) -> list[AdifQso]:
    """Parse ADIF text into a list of QSO records."""
    # Skip the optional header, which ends at <EOH>.
    body = text
    eoh = re.search(r"<eoh>", text, re.IGNORECASE)
    if eoh:
        body = text[eoh.end():]

    records: list[AdifQso] = []
    current: dict[str, str] = {}
    pos = 0

    while True:
        match = TAG_RE.search(body, pos)
        if not match:
            break

        tag = match.group(1).lower()
        length_str = match.group(2)

        if tag == "eor":
            if current:
                records.append(AdifQso(fields=current))
            current = {}
            pos = match.end()
            continue

        if length_str is None:
            # A tag with no length (shouldn't normally happen for data
            # fields) -- skip past it rather than misreading the file.
            pos = match.end()
            continue

        length = int(length_str)
        start = match.end()
        current[tag] = body[start:start + length].strip()
        pos = start + length

    if current:
        records.append(AdifQso(fields=current))

    return records


def distinct_callsigns(qsos: list[AdifQso]) -> list[str]:
    """Unique callsigns from a list of QSOs, in first-seen order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for qso in qsos:
        cs = qso.callsign
        if cs and cs not in seen:
            seen.add(cs)
            ordered.append(cs)
    return ordered
