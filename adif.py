"""
Tiny ADIF (.adi) parser.

We only need enough of the ADIF spec to pull distinct callsigns (plus a
little context per QSO) out of a log file -- not a full round-trip
parser/writer.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

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


# A real field boundary: <name:length>, <name:length:type>, <eor>, <eoh>.
# A bare "<" is not enough -- an over-read can land on one by coincidence.
_BOUNDARY_RE = re.compile(r"<(?:\w+:\d+(?::\w+)?|eor|eoh)>", re.IGNORECASE)


def _aligned(body: str, end: int) -> bool:
    """True if a well-formed tag starts at `end` (optional whitespace
    first), or nothing but whitespace is left. A value of this length
    then ends exactly where the next field begins."""
    if end > len(body):
        return False
    rest = body[end:]
    if rest.strip() == "":
        return True
    skipped = re.match(r"\s*", rest)
    return _BOUNDARY_RE.match(body, end + (skipped.end() if skipped else 0)) is not None


def _value_end(body: str, start: int, length: int) -> tuple[int, bool]:
    """Where a field value of declared `length` ends, and whether the
    declared length had to be ignored.

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
    mid-value, essentially never on a valid tag). Else the character
    reading, if that lands on a tag.

    If neither reading lands on a tag (the length is simply wrong: off
    by one, a truncated multibyte character, an entity the exporter
    counted differently), take the value up to the next well-formed tag.
    The second return value is True in that case so the caller can log
    which record it was. Returning the character end anyway used to
    swallow the next field with no warning, and <eor> could go with it."""
    char_end = start + length
    taken = 0
    byte_end = start
    while byte_end < len(body) and taken < length:
        taken += len(body[byte_end].encode("utf-8"))
        byte_end += 1
    if byte_end != char_end and taken == length and _aligned(body, byte_end):
        return byte_end, False
    if _aligned(body, char_end):
        return char_end, False
    nxt = _BOUNDARY_RE.search(body, start)
    if nxt and nxt.start() > start:
        return nxt.start(), True
    return char_end, False


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
        end, repaired = _value_end(body, start, length)
        if repaired:
            logger.warning(
                "ADIF field %s declared length %s did not land on the next tag; "
                "read up to the next tag instead (call=%s logid=%s)",
                tag,
                length,
                current.get("call", ""),
                current.get("app_qrzlog_logid", ""),
            )
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
