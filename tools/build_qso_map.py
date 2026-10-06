"""
Build the two data files behind the QSO Map page (/admin/qso-map).
Run by hand only when refreshing the map data -- the app never runs it.

Inputs (download them yourself; none are vendored except the outputs):
  --subunits  Natural Earth 1:50m "admin 0 map subunits" GeoJSON
              (ne_50m_admin_0_map_subunits.json, public domain). Subunits
              rather than countries because they already split out most
              of what DXCC counts separately: Alaska, Hawaii, England /
              Scotland / Wales / N. Ireland, Canary / Balearic Islands,
              Corsica, Sardinia, Sicily, Azores, Madeira, Svalbard ...
  --cty       A current cty.dat (AD1C's country file): each entity's
              name, primary prefix, and reference lat/lon. (Note cty.dat
              longitudes are positive WEST -- flipped here.)
  --arrl      Optional: dxcc.json from github.com/k0swe/dxcc-json -- adds
              the ARRL entity names as aliases, so a QSO whose COUNTRY
              field uses the ARRL spelling still lands on the map.

Outputs:
  static/qso_map/areas.geojson   simplified polygons, one feature per
                                 subunit, `id` = "a<n>", `name`.
  qso_map_entities.json          {"entities": {cty name: {lat, lon,
                                 area, prefix, aliases}}} -- `area` is
                                 the feature id the entity's reference
                                 point falls in (or within ~60 km of),
                                 null when it's off every polygon (small
                                 islands): those get a dot on the map.

Pure Python on purpose (no shapely): ray-casting point-in-polygon and
Douglas-Peucker simplification are a few lines each.
"""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIMPLIFY_DEG = 0.03      # Douglas-Peucker tolerance, degrees
ROUND = 2                # decimal places kept (~1 km)
NEAR_KM = 60             # attach an off-polygon point to a polygon this close
MIN_RING_POINTS = 4

# Hand fixes where the automatic placement gets it wrong:
# entity -> the polygon names that make up its area ([] = always a dot).
MANUAL_POLYGONS = {
    "N.Z. Subantarctic Is.": ["New Zealand SubAntarctic islands"],
    "Andaman & Nicobar Is.": ["Andaman Islands", "Nicobar Islands"],
    # No polygon at 1:50m; the point falls in Northern Cyprus otherwise.
    "UK Base Areas on Cyprus": [],
}
# Polygons never folded into a neighbour's area (disputed; DXCC
# placement depends on the operator's callsign, not the map).
NO_GROUPING = {"Crimea"}


# --- geometry ---------------------------------------------------------------

def _dp(points, tol):
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = points[a]
        bx, by = points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy)
        worst, idx = -1.0, -1
        for i in range(a + 1, b):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > worst:
                worst, idx = d, i
        if worst > tol and idx > 0:
            keep[idx] = True
            stack.append((a, idx))
            stack.append((idx, b))
    return [p for p, k in zip(points, keep) if k]


def _simplify_ring(ring):
    out = _dp(ring, SIMPLIFY_DEG)
    out = [[round(x, ROUND), round(y, ROUND)] for x, y in out]
    dedup = [out[0]]
    for p in out[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    if dedup[0] != dedup[-1]:
        dedup.append(dedup[0])
    return dedup


def _polygons(geom):
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return geom["coordinates"]
    return []


def _in_ring(x, y, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def _in_polygon(x, y, poly):
    return _in_ring(x, y, poly[0]) and not any(_in_ring(x, y, h) for h in poly[1:])


def _km_to_ring(lon, lat, ring):
    """Rough shortest distance (km) from a point to a ring's edges --
    equirectangular, plenty for a 60 km threshold."""
    k = math.cos(math.radians(lat))
    best = float("inf")
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        ax, ay = (x1 - lon) * k, y1 - lat
        bx, by = (x2 - lon) * k, y2 - lat
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best * 111.2


# --- cty.dat ----------------------------------------------------------------

def parse_cty(text: str) -> list[dict]:
    out = []
    for block in text.split(";"):
        block = block.strip()
        if not block or ":" not in block:
            continue
        head = block.split(":")
        if len(head) < 9:
            continue
        name, _cq, _itu, _cont, lat, lon, _tz, prefix = (h.strip() for h in head[:8])
        if prefix.startswith("*"):
            continue  # WAE-only / CQ-only entities, not DXCC
        out.append({
            "name": name,
            "prefix": prefix,
            "lat": float(lat),
            "lon": -float(lon),
        })
    return out


def arrl_aliases(arrl_path: Path | None, cty: list[dict]) -> dict[str, list[str]]:
    """cty name -> [ARRL name] by matching the cty primary prefix
    against each current ARRL entity's prefix regex."""
    if not arrl_path:
        return {}
    data = json.loads(arrl_path.read_text())["dxcc"]
    current = [e for e in data if not e.get("deleted") and e.get("prefixRegex")]
    cty_names = {c["name"] for c in cty}
    out = {}
    for c in cty:
        probe = c["prefix"].split("/")[0] + "AA"
        hits = [e["name"] for e in current if re.match(e["prefixRegex"], probe)]
        # Never alias to a name that is itself another cty entity (the
        # ARRL regex for IS0 also matches Italy's), or a QSO logged as
        # "Italy" could resolve to Sardinia.
        if len(hits) == 1 and hits[0] != c["name"] and hits[0] not in cty_names:
            out[c["name"]] = hits
    return out


# --- main -------------------------------------------------------------------

def _norm(name: str) -> str:
    s = name.lower().replace("&", " and ").replace("st.", "saint ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(island|islands|is|the|of|and)\b", " ", s)
    return " ".join(s.split())


def _ring_area(ring) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:]))) / 2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--subunits", required=True, type=Path)
    ap.add_argument("--cty", required=True, type=Path)
    ap.add_argument("--arrl", type=Path)
    args = ap.parse_args()

    src = json.loads(args.subunits.read_text())
    features = []
    raw = {}  # fid -> {name, admin, polys, size}
    for n, f in enumerate(src["features"]):
        props = f["properties"]
        polys = _polygons(f["geometry"])
        if not polys:
            continue
        fid = f"a{n}"
        name = props.get("SUBUNIT") or props.get("NAME")
        raw[fid] = {
            "name": name,
            "admin": props.get("ADMIN") or name,
            "polys": polys,
            "size": sum(_ring_area(p[0]) for p in polys),
        }
        simple = []
        for poly in polys:
            rings = [_simplify_ring(r) for r in poly]
            rings = [r for r in rings if len(r) >= MIN_RING_POINTS]
            if rings and len(rings[0]) >= MIN_RING_POINTS:
                simple.append(rings)
        if not simple:
            # A speck that simplified away -- keep its outer ring raw.
            simple = [[[[round(x, ROUND), round(y, ROUND)] for x, y in polys[0][0]]]]
        features.append({
            "type": "Feature",
            "id": fid,
            "properties": {"name": name},
            "geometry": {"type": "MultiPolygon", "coordinates": simple},
        })

    cty = parse_cty(args.cty.read_text(encoding="latin-1"))
    aliases = arrl_aliases(args.arrl, cty)

    def nearest(lon, lat, fids, limit_km):
        best = (limit_km, None)
        for fid in fids:
            for p in raw[fid]["polys"]:
                d = 0.0 if _in_polygon(lon, lat, p) else _km_to_ring(lon, lat, p[0])
                if d < best[0]:
                    best = (d, fid)
        return best[1]

    # 1) A polygon named like the entity (Gibraltar, British Virgin
    #    Islands, Vatican ...) wins if it's anywhere near the reference
    #    point -- cty.dat coordinates are coarse, and a tiny territory's
    #    point often sits in its big neighbour. 2) Otherwise the polygon
    #    the point is in, 3) else the nearest within NEAR_KM, 4) else a dot.
    by_name = {}
    for fid, r in raw.items():
        by_name.setdefault(_norm(r["name"]), []).append(fid)

    feature_of = {}
    extra_members = {}  # entity -> further polygon ids in its area
    for c in cty:
        lon, lat = c["lon"], c["lat"]
        if c["name"] in MANUAL_POLYGONS:
            fids = [fid for nm in MANUAL_POLYGONS[c["name"]] for fid in by_name.get(_norm(nm), [])]
            feature_of[c["name"]] = fids[0] if fids else None
            extra_members[c["name"]] = fids[1:]
            continue
        names = {_norm(c["name"])} | {_norm(a) for a in aliases.get(c["name"], [])}
        named = [fid for nm in names for fid in by_name.get(nm, [])]
        fid = nearest(lon, lat, named, 400) if named else None
        if fid is None:
            fid = nearest(lon, lat, raw, NEAR_KM)
        feature_of[c["name"]] = fid

    # Group polygons into clickable "areas". A polygon with no entity of
    # its own (Hokkaido, South Island, Flanders, Tobago ...) joins the
    # largest polygon of the same country that does have one, so
    # clicking anywhere in Japan opens Japan.
    has_entity = {fid for fid in feature_of.values() if fid}
    area_of = {fid: fid for fid in has_entity}
    for name, fids in extra_members.items():
        for fid in fids:
            area_of[fid] = feature_of[name]
    for fid, r in raw.items():
        if fid in area_of:
            continue
        if r["name"] in NO_GROUPING:
            area_of[fid] = fid
            continue
        siblings = [s for s in has_entity if raw[s]["admin"] == r["admin"]]
        area_of[fid] = max(siblings, key=lambda s: raw[s]["size"]) if siblings else fid
    for f in features:
        f["properties"]["area"] = area_of[f["id"]]

    entities = {}
    for c in cty:
        fid = feature_of[c["name"]]
        entities[c["name"]] = {
            "lat": round(c["lat"], 2),
            "lon": round(c["lon"], 2),
            "area": area_of[fid] if fid else None,
            "prefix": c["prefix"],
            "aliases": aliases.get(c["name"], []),
        }

    out_dir = ROOT / "static" / "qso_map"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "areas.geojson").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"))
    )
    (ROOT / "qso_map_entities.json").write_text(
        json.dumps({"source": args.cty.name, "entities": entities}, indent=0, sort_keys=True)
    )
    dots = sum(1 for e in entities.values() if not e["area"])
    print(f"{len(features)} polygons; {len(entities)} entities ({len(entities) - dots} on a polygon, {dots} as dots)")


if __name__ == "__main__":
    main()
