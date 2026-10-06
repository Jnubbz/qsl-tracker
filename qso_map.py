"""
QSO Map -- puts Josh's logged QSOs on a world map by DXCC entity
(admin page /admin/qso-map, see app.py).

Data (both built offline by tools/build_qso_map.py, checked in):
  static/qso_map/areas.geojson  Natural Earth 1:50m map subunits,
                                simplified. Each polygon carries an
                                `area` id; polygons sharing an id are
                                one clickable area (all of Japan, both
                                NZ islands ...).
  qso_map_entities.json         every current DXCC entity from cty.dat:
                                reference lat/lon, the `area` it sits in
                                (null = too small for the map, drawn as
                                a dot), and ARRL-name aliases.

Each QSO's stored `dxcc_entity` is whatever its ADIF/QRZ COUNTRY field
said (or a prefix guess, see dxcc.py), so its spelling can differ from
cty.dat's. resolve_entities() maps every stored name to a cty entity:
exact-ish name match first, then aliases, then -- for names it still
can't place -- a majority vote of dxcc.entity_for_callsign() over that
name's callsigns. Anything left over is listed under the map as "not
on the map" (still clickable), so no QSO is ever hidden.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from functools import lru_cache
from pathlib import Path

import dxcc

_DATA_PATH = Path(__file__).with_name("qso_map_entities.json")

# Spellings seen in ADIF/QRZ COUNTRY fields and older cty.dat files
# that the generic normalisation below doesn't already line up.
_EXTRA_ALIASES = {
    "United States": ["USA", "United States of America", "U.S.A."],
    "Fed. Rep. of Germany": ["Germany", "Federal Republic of Germany"],
    "European Russia": ["Russia (European)", "Russia European"],
    "Asiatic Russia": ["Russia (Asiatic)", "Russia Asiatic"],
    "Asiatic Turkey": ["Turkey", "Turkiye", "Türkiye"],
    "Republic of Korea": ["South Korea", "Korea"],
    "DPR of Korea": ["North Korea"],
    "Czech Republic": ["Czechia"],
    "Slovak Republic": ["Slovakia"],
    "Kingdom of eSwatini": ["Swaziland", "Eswatini"],
    "North Macedonia": ["Macedonia", "FYRO Macedonia"],
    "Timor - Leste": ["East Timor", "Timor-Leste"],
    "Cote d'Ivoire": ["Ivory Coast"],
    "Vatican City": ["Vatican"],
    "Bosnia-Herzegovina": ["Bosnia and Herzegovina"],
    "Myanmar": ["Burma"],
    "Palestine": ["Palestinian Territories"],
    "Sov Mil Order of Malta": ["Sovereign Military Order of Malta", "SMOM"],
    "Hong Kong": ["Hong Kong S.A.R."],
    "Mariana Islands": ["Northern Mariana Islands"],
    "Micronesia": ["Federated States of Micronesia"],
    "Republic of Kosovo": ["Kosovo"],
    "Republic of South Sudan": ["South Sudan"],
    "Dem. Rep. of the Congo": ["Democratic Republic of the Congo", "DR Congo", "Congo (Kinshasa)"],
    "Republic of the Congo": ["Congo", "Congo (Brazzaville)"],
}

_DROP_WORDS = r"\b(island|islands|is|isl|the|of|and|republic|rep|federal|fed|kingdom)\b"


def norm(name: str) -> str:
    """Loose comparison key for entity names: case, punctuation, "&"
    vs "and", "St." vs "Saint", and filler words ("Islands", "Rep.
    of", ...) all ignored."""
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"\bst\.?(?=\s)", "saint", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(_DROP_WORDS, " ", s)
    return " ".join(s.split())


@lru_cache(maxsize=1)
def entities() -> dict[str, dict]:
    with _DATA_PATH.open() as f:
        return json.load(f)["entities"]


@lru_cache(maxsize=1)
def _name_index() -> dict[str, str]:
    """norm(any known spelling) -> cty entity name. Real cty names win
    over aliases if two ever collide."""
    index: dict[str, str] = {}
    for name, info in entities().items():
        for alias in info.get("aliases", []) + _EXTRA_ALIASES.get(name, []):
            index.setdefault(norm(alias), name)
    for name in entities():
        index[norm(name)] = name
    return index


def entity_by_name(name: str) -> str | None:
    return _name_index().get(norm(name)) if name else None


def resolve_entities(qsos: list[dict]) -> dict[str, str | None]:
    """{stored dxcc_entity string (including "") -> cty entity name, or
    None when it can't be placed}."""
    by_stored: dict[str, list[dict]] = {}
    for q in qsos:
        by_stored.setdefault(q.get("dxcc_entity") or "", []).append(q)

    out: dict[str, str | None] = {}
    for stored, rows in by_stored.items():
        hit = entity_by_name(stored)
        if hit is None:
            votes = Counter()
            for q in rows[:200]:
                guess = entity_by_name(dxcc.entity_for_callsign(q.get("callsign", "")) or "")
                if guess:
                    votes[guess] += 1
            if votes:
                hit = votes.most_common(1)[0][0]
        out[stored] = hit
    return out


def group_key(stored: str, resolved: str | None) -> str:
    """The id a panel uses for one entity's QSOs: the cty name when
    resolved, else the raw stored string marked with a leading "?"."""
    return resolved if resolved else "?" + stored


def summary(qsos: list[dict]) -> dict:
    """Everything the map needs up front, without any QSO detail:
      areas:    {area_id: {"count": n, "entities": [[entity, n], ...]}}
      dots:     [{"key", "name", "lat", "lon", "count"}] -- entities
                without a polygon (worked or not, so unworked islands
                show as faint dots too)
      unplaced: [{"key", "name", "count"}] -- QSOs whose entity can't
                be found at all
      worked / total: DXCC entities with at least one QSO / all current
    """
    ents = entities()
    resolved = resolve_entities(qsos)
    counts: Counter = Counter()
    for q in qsos:
        stored = q.get("dxcc_entity") or ""
        counts[group_key(stored, resolved[stored])] += 1

    areas: dict[str, dict] = {}
    dots = []
    for name, info in ents.items():
        n = counts.get(name, 0)
        if info.get("area"):
            if n:
                area = areas.setdefault(info["area"], {"count": 0, "entities": []})
                area["count"] += n
                area["entities"].append([name, n])
            else:
                areas.setdefault(info["area"], {"count": 0, "entities": []})
        else:
            dots.append({"key": name, "name": name, "lat": info["lat"], "lon": info["lon"], "count": n})
    for area in areas.values():
        area["entities"].sort(key=lambda e: -e[1])

    unplaced = [
        {"key": key, "name": key[1:] or "(no entity recorded)", "count": n}
        for key, n in counts.items() if key.startswith("?")
    ]
    unplaced.sort(key=lambda u: -u["count"])

    return {
        "areas": areas,
        "dots": dots,
        "unplaced": unplaced,
        "worked": sum(1 for name in ents if counts.get(name)),
        "total": len(ents),
        "qsos": len(qsos),
    }


def area_entities(area_id: str) -> list[str]:
    """cty entity names inside one clickable area."""
    return sorted(name for name, info in entities().items() if info.get("area") == area_id)


def qsos_for_keys(qsos: list[dict], keys: set[str]) -> dict[str, list[dict]]:
    """{group key -> its QSOs} for the requested keys (entity names or
    "?raw" keys), preserving the caller's order (newest first)."""
    resolved = resolve_entities(qsos)
    out: dict[str, list[dict]] = {k: [] for k in keys}
    for q in qsos:
        stored = q.get("dxcc_entity") or ""
        key = group_key(stored, resolved[stored])
        if key in out:
            out[key].append(q)
    return out
