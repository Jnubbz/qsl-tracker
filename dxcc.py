"""
DXCC entity lookup -- a best-effort fallback for when a QSO's ADIF
record doesn't already carry a country/DXCC field.

Data (dxcc_prefixes.json, checked into the repo alongside this module)
was converted from a public-domain-style cty.dat country file (the
same kind of prefix table essentially every contest logger and DX
cluster tool uses), a 2008 snapshot. Prefix-to-entity assignments for
the great majority of active DXCC entities have been stable for
decades, so this stays a reasonable fallback -- but it is NOT
authoritative and can be wrong or stale for:
  - Entities added, retired, or renamed after ~2008 (a handful of
    well-known post-2008 renames are patched in directly -- see
    _RENAMES below -- but a genuinely new entity added since then
    just won't be in the table).
  - Special-event / unusual callsigns that don't follow their
    country's normal prefix pattern (some real exceptions ARE baked
    into the source data as exact-callsign overrides, but not all).
  - Portable operation ("/") where the location indicator is
    ambiguous or omitted.

This is why ADIF's own COUNTRY field (when Josh's logging software
provides it) is always preferred over this derivation -- see
photomap_store.import_my_qsos().
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_DATA_PATH = Path(__file__).with_name("dxcc_prefixes.json")

# Suffixes that mark an *operating mode*, not a change of location --
# never treated as a portable-location indicator even though some of
# them (e.g. "M", "P") happen to also be valid prefixes for other
# countries (M0ABC/M1ABC are real UK calls, which is exactly the trap
# this guards against: W1ABC/M must not resolve to "England").
_MODE_SUFFIXES = {
    "M", "P", "MM", "AM", "A", "B", "QRP", "LH", "R", "BCN", "PM",
}

_MAX_PREFIX_LEN = 6


def _load() -> tuple[dict[str, str], dict[str, str]]:
    with _DATA_PATH.open() as f:
        data = json.load(f)
    return data["exact"], data["prefixes"]


_EXACT, _PREFIXES = _load()


def _prefix_match(token: str) -> tuple[str, int] | None:
    """Longest-prefix match of `token` against the prefix table.
    Returns (entity_name, matched_length) or None."""
    token = token.upper()
    for length in range(min(len(token), _MAX_PREFIX_LEN), 0, -1):
        hit = _PREFIXES.get(token[:length])
        if hit:
            return hit, length
    return None


def entity_for_callsign(callsign: str) -> str | None:
    """Best-effort DXCC entity name for a callsign, or None if nothing
    in the table matches. Handles the common "/"-portable convention
    (e.g. W1ABC/KH6 -> Hawaii) while ignoring pure operating-mode
    suffixes (W1ABC/M, W1ABC/QRP, W1ABC/4 -- a call-area digit alone
    never changes the entity)."""
    if not callsign:
        return None
    callsign = callsign.strip().upper()
    # Strip anything that isn't part of the call structure (spaces,
    # stray punctuation some logs include) -- keep letters, digits, /.
    callsign = re.sub(r"[^A-Z0-9/]", "", callsign)
    if not callsign:
        return None

    parts = [p for p in callsign.split("/") if p]
    if not parts:
        return None

    # Exact full-callsign override -- check the plain call and every
    # slash-part as-is before falling back to prefix matching.
    for candidate in [callsign.replace("/", "")] + parts:
        hit = _EXACT.get(candidate)
        if hit:
            return hit

    candidates = [
        p for p in parts
        if p not in _MODE_SUFFIXES and not p.isdigit()
    ]
    if not candidates:
        candidates = parts[:1]

    best: tuple[str, int] | None = None
    for part in candidates:
        match = _prefix_match(part)
        if match and (best is None or match[1] > best[1]):
            best = match

    return best[0] if best else None
