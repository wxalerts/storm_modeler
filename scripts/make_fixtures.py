"""Generate the deterministic replay fixtures for Section 7.

Two cases, each a self-contained directory of ``warning.json`` + pre-gridded
``volumes/*.npz``:

* ``tornado_warning_case`` — a KFWS tornado warning with a deep, tracked
  convective cell (validation A: cells join to the warning with realistic
  depth).
* ``ap_case`` — a KHGX warning whose radar is pure anomalous propagation at
  2026-06-24 11:41:00Z, so SCIT admits zero cells (validation B).

Everything is closed-form and clock-free, so re-running yields identical
fixtures.

Run:  uv run python scripts/make_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

from shapely.geometry import Polygon, mapping

from storm_modeler.config import FIXTURE_DIR
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_ap_volume, make_storm_volume


def _box(lon, lat, dlon=0.30, dlat=0.30) -> Polygon:
    return Polygon(
        [
            (lon - dlon, lat - dlat),
            (lon + dlon, lat - dlat),
            (lon + dlon, lat + dlat),
            (lon - dlon, lat + dlat),
        ]
    )


def _write_warning(path: Path, **fields) -> None:
    fields["polygon"] = mapping(fields["polygon"])
    path.write_text(json.dumps(fields, indent=2))


def build_tornado_case(root: Path) -> None:
    d = root / "tornado_warning_case"
    (d / "volumes").mkdir(parents=True, exist_ok=True)
    s = get_site("KFWS")
    poly = _box(s.lon + 0.15, s.lat + 0.07, 0.35, 0.30)

    _write_warning(
        d / "warning.json",
        id="2024-05-25-FWD-TOW-0084",
        event="Tornado Warning",
        phenomena="TO",
        significance="W",
        wfo="FWD",
        etn=84,
        ugc=["TXC201", "TXC439"],
        states=["TX"],
        polygon=poly,
        issued="2024-05-25T11:41:00Z",
        expires="2024-05-25T12:15:00Z",
    )

    # Nine volumes within issued-60..expires+30 (10:41..12:45), every ~6 min;
    # a single cell tracking NE. Enough volumes to exercise --cancel-after.
    steps = [
        ("2024-05-25T11:42:00Z", 0.06, 0.02),
        ("2024-05-25T11:48:00Z", 0.10, 0.05),
        ("2024-05-25T11:54:00Z", 0.14, 0.08),
        ("2024-05-25T12:00:00Z", 0.18, 0.11),
        ("2024-05-25T12:06:00Z", 0.22, 0.14),
        ("2024-05-25T12:12:00Z", 0.26, 0.17),
        ("2024-05-25T12:18:00Z", 0.30, 0.20),
        ("2024-05-25T12:24:00Z", 0.34, 0.23),
        ("2024-05-25T12:30:00Z", 0.38, 0.26),
    ]
    for i, (t, dlon, dlat) in enumerate(steps, 1):
        vol = make_storm_volume(
            "KFWS", s.lat, s.lon, t,
            core_lon=s.lon + dlon, core_lat=s.lat + dlat,
            peak_dbz=57.5, echo_top_km=9.7,
        )
        vol.save_npz(d / "volumes" / f"v_{i:03d}.npz")
    print(f"tornado_warning_case -> {d}")


def build_ap_case(root: Path) -> None:
    d = root / "ap_case"
    (d / "volumes").mkdir(parents=True, exist_ok=True)
    s = get_site("KHGX")
    poly = _box(s.lon + 0.05, s.lat + 0.10, 0.30, 0.30)

    _write_warning(
        d / "warning.json",
        id="2026-06-24-HGX-SVW-0117",
        event="Severe Thunderstorm Warning",
        phenomena="SV",
        significance="W",
        wfo="HGX",
        etn=117,
        ugc=["TXC201"],
        states=["TX"],
        polygon=poly,
        issued="2026-06-24T11:00:00Z",
        expires="2026-06-24T11:45:00Z",
    )

    # The audited volume: pure AP at exactly 11:41:00Z (validation B timestamp).
    vol = make_ap_volume(
        "KHGX", s.lat, s.lon, "2026-06-24T11:41:00Z",
        core_lon=s.lon + 0.05, core_lat=s.lat + 0.10,
        peak_dbz=52.0,
    )
    vol.save_npz(d / "volumes" / "v_001.npz")
    print(f"ap_case -> {d}")


def main() -> None:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    build_tornado_case(FIXTURE_DIR)
    build_ap_case(FIXTURE_DIR)


if __name__ == "__main__":
    main()
