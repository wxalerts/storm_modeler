"""Generate the shipped vector basemap layers (GeoJSON).

Produces coarse but geographically real states / counties / highways layers for
the CONUS south-central region that the Phase-A fixtures live in (Texas and
neighbours). These are the "shipped shapefiles" the map pane renders as
polylines — committed so the GUI needs no network and no tile server.

Run:  uv run python scripts/make_basemap.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "shapefiles"

# Recognisable simplified Texas outline (lon, lat), plus neighbour boxes.
TEXAS = [
    (-103.04, 36.50), (-100.00, 36.50), (-100.00, 34.56), (-99.20, 34.21),
    (-97.46, 33.82), (-95.30, 33.87), (-94.04, 33.55), (-94.04, 31.00),
    (-93.50, 30.30), (-93.83, 29.80), (-95.10, 29.10), (-96.50, 28.40),
    (-97.10, 27.80), (-97.40, 26.00), (-97.14, 25.84), (-99.10, 26.40),
    (-100.30, 28.20), (-101.50, 29.80), (-102.30, 29.90), (-103.00, 29.00),
    (-104.50, 29.70), (-106.50, 31.80), (-106.62, 31.91), (-103.04, 32.00),
    (-103.04, 36.50),
]


def _box(lon0, lat0, lon1, lat1):
    return [
        (lon0, lat0), (lon1, lat0), (lon1, lat1), (lon0, lat1), (lon0, lat0)
    ]


STATE_RINGS = {
    "TX": TEXAS,
    "OK": _box(-103.00, 33.62, -94.43, 37.00),
    "NM": _box(-109.05, 31.33, -103.00, 37.00),
    "LA": _box(-94.04, 29.00, -89.00, 33.02),
    "AR": _box(-94.62, 33.00, -89.64, 36.50),
}

HIGHWAYS = {
    "I-35": [(-99.50, 27.52), (-98.49, 29.42), (-97.74, 30.27),
             (-97.13, 31.55), (-97.33, 32.74), (-97.03, 33.20)],
    "I-45": [(-95.36, 29.76), (-96.05, 30.55), (-96.47, 31.10),
             (-96.64, 31.90), (-96.85, 32.78)],
    "I-10": [(-106.49, 31.76), (-104.50, 30.70), (-103.49, 30.80),
             (-101.50, 30.20), (-100.00, 29.50), (-98.49, 29.42),
             (-96.00, 29.70), (-95.36, 29.76), (-94.04, 30.10)],
    "I-20": [(-103.00, 32.00), (-102.08, 31.99), (-100.49, 32.41),
             (-98.50, 32.55), (-97.03, 32.74), (-95.30, 32.50), (-94.04, 32.50)],
}


def _feature(geom_type, coords, props):
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": geom_type, "coordinates": coords},
    }


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


def build_states():
    feats = [
        _feature("Polygon", [[list(p) for p in ring]], {"state": st})
        for st, ring in STATE_RINGS.items()
    ]
    return _fc(feats)


def build_highways():
    feats = [
        _feature("LineString", [list(p) for p in pts], {"route": name})
        for name, pts in HIGHWAYS.items()
    ]
    return _fc(feats)


def build_counties():
    """A 0.5-degree graticule over the region — representative county lines."""
    lon0, lon1 = -107.0, -93.0
    lat0, lat1 = 25.0, 37.0
    feats = []
    lon = lon0
    while lon <= lon1 + 1e-6:
        feats.append(_feature("LineString", [[lon, lat0], [lon, lat1]], {"k": "meridian"}))
        lon += 0.5
    lat = lat0
    while lat <= lat1 + 1e-6:
        feats.append(_feature("LineString", [[lon0, lat], [lon1, lat]], {"k": "parallel"}))
        lat += 0.5
    return _fc(feats)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "states.geojson").write_text(json.dumps(build_states()))
    (OUT / "counties.geojson").write_text(json.dumps(build_counties()))
    (OUT / "highways.geojson").write_text(json.dumps(build_highways()))
    print(f"wrote basemap layers to {OUT}")


if __name__ == "__main__":
    main()
