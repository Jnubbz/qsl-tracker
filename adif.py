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


_NEXT_TAG_RE = re.compile(r"\s*(?:<\w+:\d+(?::\w+)?>|<eor>|<eoh>|$)", re.IGNORECASE)


def _aligned(body: str, end: int) -> bool:
    """True if a well-formed tag (<name:len>, <eor>, <eoh>) or the end of
    the text starts right after `end`, give or take whitespace -- i.e. a
    value of this length ends exactly where the next field begins. A bare
    "<" isn't enough: an over-read can land on one by coincidence."""
    return _NEXT_TAG_RE.match(body, end) is not None


def _value_end(body: str, start: int, length: int) -> int:
    """Where a field value of declared `length` ends.

    The ADIF spec counts characters, but plenty of exporters (QRZ's
    Logbook API among them) count UTF-8 *bytes* -- so "José" is declared
    5 and a Japanese name like 山田 is declared 6 (3 bytes per kanji).
    Reading by characters then over-reads into the following tags,
    swallows the record's <eor>, and the whole QSO silently vanishes
    (found 2026-10-04 chasing a 28-QSO gap with QRZ: Japanese and
    accented names).

    For an all-ASCII value both readings are the same. Otherwise: the
    byte reading is shorter, and if it lands exactly on a well-formed
    tag it's right (for a genuinely character-counted file it would land
    mid-value, essentially never on a valid tag). Else fall back to the
    character reading."""
    char_end = start + length
    taken = 0
    byte_end = start
    while byte_end < len(body) and taken < length:
        taken += len(body[byte_end].encode("utf-8"))
        byte_end += 1
    if byte_end != char_end and taken == length and _aligned(body, byte_end):
        return byte_end
    return char_end


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
        end = _value_end(body, start, length)
        current[tag] = body[start:end].strip()
        pos = end

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
