"""Deterministic synthetic gridded volumes.

Used to build the offline replay fixtures and unit tests. Everything here is a
closed-form function of its arguments — no RNG, no clock — so a given call
always yields byte-identical grids, preserving the determinism mandate.

Two builders matter:

* :func:`make_storm_volume` — a vertically deep convective core that SCIT must
  admit (high reflectivity from the surface up to ~10 km).
* :func:`make_ap_volume` — anomalous propagation: strong returns confined to
  the lowest grid level, which SCIT must reject (zero admitted cells).
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
from pyproj import Transformer

from ..config import DEFAULT_GRID, GridConfig
from ..models import FreezingLevelGrid, GriddedVolume, SatelliteScene

# Default fixture grid spacing (km). Matches the registry grid_h_km / grid_v_km
# defaults; kept here so synthetic volumes need no settings store.
H_KM = 1.0
V_KM = 0.5


def _axes(grid: GridConfig, h_km: float = H_KM, v_km: float = V_KM):
    hw = grid.half_width_km * 1000.0
    sp = h_km * 1000.0
    x = np.arange(-hw, hw + sp / 2, sp, dtype=np.float64)
    y = np.arange(-hw, hw + sp / 2, sp, dtype=np.float64)
    z = np.arange(grid.z_min_km, grid.z_max_km + v_km / 2, v_km) * 1000.0
    return x, y, z


def _lonlat_to_xy(lat0: float, lon0: float, lon: float, lat: float):
    aeqd = (
        f"+proj=aeqd +lat_0={lat0} +lon_0={lon0} +x_0=0 +y_0=0 "
        "+datum=WGS84 +units=m +no_defs"
    )
    t = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
    return t.transform(lon, lat)


def make_storm_volume(
    site: str,
    lat0: float,
    lon0: float,
    valid_time: datetime | str,
    core_lon: float,
    core_lat: float,
    peak_dbz: float = 57.0,
    echo_top_km: float = 10.5,
    radius_km: float = 8.0,
    grid: GridConfig = DEFAULT_GRID,
) -> GriddedVolume:
    """A single deep convective storm centred at ``(core_lon, core_lat)``.

    The reflectivity declines with horizontal distance (Gaussian, scale
    ``radius_km``) and with height (linear from a low-level peak down to 0 dBZ
    near ``echo_top_km``), so the >=18.3 dBZ echo reaches ~``echo_top_km`` and
    the >=40 dBZ core spans many levels — a textbook admit.
    """
    x, y, z = _axes(grid)
    cx, cy = _lonlat_to_xy(lat0, lon0, core_lon, core_lat)

    xx, yy = np.meshgrid(x, y)  # (ny, nx)
    dist = np.hypot(xx - cx, yy - cy) / 1000.0  # km
    horiz = np.exp(-(dist / radius_km) ** 2)  # (ny, nx) in [0,1]

    z_km = z / 1000.0
    peak_z = 1.5  # km, height of the reflectivity peak
    echo_top_dbz = 18.3  # the contour that defines echo top
    # Decline so the 18.3 dBZ contour reaches exactly ``echo_top_km``.
    slope = (peak_dbz - echo_top_dbz) / (echo_top_km - peak_z)
    prof = np.where(
        z_km <= peak_z,
        peak_dbz - 2.0 * (peak_z - z_km),  # mild surface taper
        peak_dbz - slope * (z_km - peak_z),
    )
    prof = np.clip(prof, 0.0, None)  # (nz,)

    refl = prof[:, None, None] * horiz[None, :, :]  # (nz, ny, nx)
    refl = np.where(refl >= 5.0, refl, np.nan).astype(np.float32)
    return GriddedVolume(
        site=site,
        valid_time=valid_time,
        reflectivity=refl,
        x=x,
        y=y,
        z=z,
        lat0=lat0,
        lon0=lon0,
    )


def make_ap_volume(
    site: str,
    lat0: float,
    lon0: float,
    valid_time: datetime | str,
    core_lon: float,
    core_lat: float,
    peak_dbz: float = 52.0,
    radius_km: float = 12.0,
    grid: GridConfig = DEFAULT_GRID,
) -> GriddedVolume:
    """Anomalous propagation: strong returns ONLY at the lowest grid level.

    No vertical structure whatsoever — the echo never appears above ``z[0]``.
    SCIT's vertical-continuity gates (min levels / depth / echo top) reject it,
    so it admits zero cells. This is the AP false-seed audit case.
    """
    x, y, z = _axes(grid)
    cx, cy = _lonlat_to_xy(lat0, lon0, core_lon, core_lat)

    xx, yy = np.meshgrid(x, y)
    dist = np.hypot(xx - cx, yy - cy) / 1000.0
    horiz = np.exp(-(dist / radius_km) ** 2)
    ground = (peak_dbz * horiz).astype(np.float32)

    refl = np.full((z.size, y.size, x.size), np.nan, dtype=np.float32)
    refl[0] = np.where(ground >= 5.0, ground, np.nan)
    return GriddedVolume(
        site=site,
        valid_time=valid_time,
        reflectivity=refl,
        x=x,
        y=y,
        z=z,
        lat0=lat0,
        lon0=lon0,
    )


def make_cold_scene(
    satellite: str,
    valid_time: datetime | str,
    bbox: tuple[float, float, float, float],
    core_lon: float,
    core_lat: float,
    anvil_radius_km: float = 30.0,
    core_radius_km: float = 6.0,
    anvil_bt_k: float = 215.0,
    core_bt_k: float = 198.0,
    warm_bt_k: float = 285.0,
    res_deg: float = 0.02,
    band: int = 13,
) -> SatelliteScene:
    """A textbook cold cloud top: a broad anvil with a colder embedded core.

    Brightness temperature falls from ``warm_bt_k`` outside the storm to
    ``anvil_bt_k`` across the anvil (Gaussian, scale ``anvil_radius_km``), with a
    sharper colder dip to ``core_bt_k`` at ``(core_lon, core_lat)`` (scale
    ``core_radius_km``) — the overshooting top. Closed-form, no RNG, so a given
    call yields byte-identical scenes (the determinism mandate). The raster runs
    north→south (row 0 = north) to match :class:`SatelliteScene`.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lons = np.arange(lon_min, lon_max + res_deg / 2, res_deg, dtype=np.float64)
    lats = np.arange(lat_max, lat_min - res_deg / 2, -res_deg, dtype=np.float64)
    LON, LAT = np.meshgrid(lons, lats)  # (nlat, nlon)

    lat_c = 0.5 * (lat_min + lat_max)
    km_per_deg_lat = 110.57
    km_per_deg_lon = 111.32 * np.cos(np.radians(lat_c))
    dist_km = np.hypot(
        (LON - core_lon) * km_per_deg_lon, (LAT - core_lat) * km_per_deg_lat
    )

    anvil = (warm_bt_k - anvil_bt_k) * np.exp(-(dist_km / anvil_radius_km) ** 2)
    core = (anvil_bt_k - core_bt_k) * np.exp(-(dist_km / core_radius_km) ** 2)
    bt = (warm_bt_k - anvil - core).astype(np.float32)
    return SatelliteScene(
        satellite=satellite,
        band=band,
        valid_time=valid_time,
        bt=bt,
        lons=lons,
        lats=lats,
        bbox=bbox,
    )


def make_freezing_grid(
    valid_time: datetime | str,
    bbox: tuple[float, float, float, float],
    level_m: float = 4000.0,
    res_deg: float = 0.03,
    model: str = "HRRR",
) -> FreezingLevelGrid:
    """A flat synthetic 0 °C freezing-level grid at ``level_m`` (metres MSL).

    Closed-form (constant field), so a given call yields byte-identical grids —
    the determinism mandate. The raster runs north→south (row 0 = north) to
    match :class:`FreezingLevelGrid`.
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    lons = np.arange(lon_min, lon_max + res_deg / 2, res_deg, dtype=np.float64)
    lats = np.arange(lat_max, lat_min - res_deg / 2, -res_deg, dtype=np.float64)
    heights = np.full((lats.size, lons.size), float(level_m), dtype=np.float32)
    return FreezingLevelGrid(
        model=model, valid_time=valid_time,
        heights_m=heights, lons=lons, lats=lats, bbox=bbox,
    )


# ---------------------------------------------------------------------------
# Replay-case builders (used by the test suite + the GUI smoke test).
#
# These write a self-contained replay directory (warning.json + volumes/*.npz)
# — the same layout DownloadStore/FixtureVolumeSource read. They are generated
# on demand into a temp dir; no synthetic data is shipped in the repo (it was
# too easily mistaken for a real download).
# ---------------------------------------------------------------------------

def _box(lon, lat, dlon=0.30, dlat=0.30):
    from shapely.geometry import Polygon

    return Polygon([
        (lon - dlon, lat - dlat), (lon + dlon, lat - dlat),
        (lon + dlon, lat + dlat), (lon - dlon, lat + dlat),
    ])


def _write_warning(path, **fields) -> None:
    import json

    from shapely.geometry import mapping

    fields["polygon"] = mapping(fields["polygon"])
    path.write_text(json.dumps(fields, indent=2))


def build_tornado_case(root):
    """A KFWS tornado warning: nine volumes, one deep cell tracking NE."""
    from pathlib import Path

    from .sites import get_site

    d = Path(root) / "tornado_warning_case"
    (d / "volumes").mkdir(parents=True, exist_ok=True)
    s = get_site("KFWS")
    poly = _box(s.lon + 0.15, s.lat + 0.07, 0.35, 0.30)
    _write_warning(
        d / "warning.json", id="2024-05-25-FWD-TOW-0084", event="Tornado Warning",
        phenomena="TO", significance="W", wfo="FWD", etn=84,
        ugc=["TXC201", "TXC439"], states=["TX"], polygon=poly,
        issued="2024-05-25T11:41:00Z", expires="2024-05-25T12:15:00Z",
    )
    steps = [
        ("2024-05-25T11:42:00Z", 0.06, 0.02), ("2024-05-25T11:48:00Z", 0.10, 0.05),
        ("2024-05-25T11:54:00Z", 0.14, 0.08), ("2024-05-25T12:00:00Z", 0.18, 0.11),
        ("2024-05-25T12:06:00Z", 0.22, 0.14), ("2024-05-25T12:12:00Z", 0.26, 0.17),
        ("2024-05-25T12:18:00Z", 0.30, 0.20), ("2024-05-25T12:24:00Z", 0.34, 0.23),
        ("2024-05-25T12:30:00Z", 0.38, 0.26),
    ]
    for i, (t, dlon, dlat) in enumerate(steps, 1):
        vol = make_storm_volume(
            "KFWS", s.lat, s.lon, t, core_lon=s.lon + dlon, core_lat=s.lat + dlat,
            peak_dbz=57.5, echo_top_km=9.7,
        )
        vol.save_npz(d / "volumes" / f"v_{i:03d}.npz")
    return d


def build_ap_case(root):
    """A KHGX warning whose single volume is pure AP (SCIT admits zero cells)."""
    from pathlib import Path

    from .sites import get_site

    d = Path(root) / "ap_case"
    (d / "volumes").mkdir(parents=True, exist_ok=True)
    s = get_site("KHGX")
    poly = _box(s.lon + 0.05, s.lat + 0.10, 0.30, 0.30)
    _write_warning(
        d / "warning.json", id="2026-06-24-HGX-SVW-0117",
        event="Severe Thunderstorm Warning", phenomena="SV", significance="W",
        wfo="HGX", etn=117, ugc=["TXC201"], states=["TX"], polygon=poly,
        issued="2026-06-24T11:00:00Z", expires="2026-06-24T11:45:00Z",
    )
    vol = make_ap_volume(
        "KHGX", s.lat, s.lon, "2026-06-24T11:41:00Z",
        core_lon=s.lon + 0.05, core_lat=s.lat + 0.10, peak_dbz=52.0,
    )
    vol.save_npz(d / "volumes" / "v_001.npz")
    return d
