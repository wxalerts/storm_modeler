"""Vertical cross-section through a storm (Phase B2).

The "orthogonal display": a vertical slice through the grid passing through the
cell's seed, rendered as a distance-vs-height dBZ panel with the shared NWS
palette. The section azimuth follows storm motion by default (the cell's track
heading), or a fixed bearing, or a manual override.

Pure geometry + a PyVista panel render (engine + palette unity with the 3D
view); no Qt. The model pane drives it; the headless ``render_cell`` tool uses
:func:`render_section` for the 8B non-empty assertion.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import structlog

from ..detection.detection_v2 import StormCell
from ..models import GriddedVolume
from ..settings.resolver import ViewerParams
from . import scene_builder as sb

log = structlog.get_logger(__name__)

# Cross-section vertical exaggeration: the panel applies its own (independent of
# the 3D ``vert_exag``) so a shallow-but-wide slice reads as a storm profile.
XSECTION_VEXAG = 3.0
# dBZ threshold defining the echo top drawn over the panel (the detection
# default continuity threshold; a display constant here).
ECHO_TOP_DBZ = 18.3


@dataclass
class Section:
    """An extracted vertical slice: dBZ on a (height, distance) panel."""

    azimuth_deg: float
    dist_km: np.ndarray          # (ndist,) signed distance from seed along az
    z_km: np.ndarray             # (nz,) height AGL
    dbz: np.ndarray              # (nz, ndist) dBZ, NaN where no echo
    seed_x_km: float
    seed_y_km: float


# ---------------------------------------------------------------------------
# Azimuth
# ---------------------------------------------------------------------------

def _track_seeds(cell: StormCell, results) -> list[tuple]:
    """Ordered (valid_time, seed_x, seed_y) for ``cell``'s track across volumes."""
    tid = cell.track_id
    pts: list[tuple] = []
    for res in results:
        for c in res.cells:
            if tid >= 0 and c.track_id == tid:
                pts.append((c.valid_time, c.seed_x, c.seed_y))
    pts.sort(key=lambda p: p[0])
    return pts


def section_azimuth(
    cell: StormCell, results, viewer: ViewerParams, manual_deg: float | None = None
) -> float:
    """Bearing (deg from north, clockwise) of the cross-section plane."""
    source = viewer.xsection_azimuth_source
    if source == "manual" and manual_deg is not None:
        return float(manual_deg) % 360.0
    if source == "fixed":
        return float(viewer.xsection_fixed_bearing) % 360.0
    if source == "track":
        pts = _track_seeds(cell, results)
        # Heading from the segment bracketing this cell's time.
        idx = next((i for i, p in enumerate(pts) if p[0] == cell.valid_time), -1)
        for a, b in ((idx, idx + 1), (idx - 1, idx)):
            if 0 <= a < len(pts) and 0 <= b < len(pts) and a != b:
                dx = pts[b][1] - pts[a][1]  # east
                dy = pts[b][2] - pts[a][2]  # north
                if math.hypot(dx, dy) > 1.0:  # > 1 m of motion
                    return math.degrees(math.atan2(dx, dy)) % 360.0
    # Fall back to the fixed bearing (default north) when motion is unknown.
    return float(viewer.xsection_fixed_bearing) % 360.0


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_section(
    volume: GriddedVolume, cell: StormCell, azimuth_deg: float, half_len_km: float
) -> Section:
    """Sample the grid along a line through the seed at ``azimuth_deg``."""
    sx, sy = cell.seed_x, cell.seed_y  # metres
    east, north = math.sin(math.radians(azimuth_deg)), math.cos(math.radians(azimuth_deg))
    dx_km = volume.dx_km
    ndist = max(int(2 * half_len_km / max(dx_km, 0.1)) + 1, 8)
    dist_km = np.linspace(-half_len_km, half_len_km, ndist)

    px = sx + dist_km * 1000.0 * east   # metres east of radar
    py = sy + dist_km * 1000.0 * north  # metres north of radar
    ix = np.clip(np.searchsorted(volume.x, px), 0, volume.x.size - 1)
    iy = np.clip(np.searchsorted(volume.y, py), 0, volume.y.size - 1)

    dbz = volume.reflectivity[:, iy, ix]  # (nz, ndist) nearest-neighbour
    return Section(
        azimuth_deg=azimuth_deg, dist_km=dist_km, z_km=volume.z / 1000.0,
        dbz=np.asarray(dbz, dtype=np.float32), seed_x_km=sx / 1000.0,
        seed_y_km=sy / 1000.0,
    )


def echo_top_profile(section: Section, thresh_dbz: float = ECHO_TOP_DBZ) -> np.ndarray:
    """Highest height (km) with dBZ ≥ ``thresh`` per distance column (NaN if none)."""
    z = section.z_km
    tops = np.full(section.dist_km.size, np.nan)
    mask = np.nan_to_num(section.dbz, nan=-999.0) >= thresh_dbz
    for i in range(section.dist_km.size):
        zi = np.nonzero(mask[:, i])[0]
        if zi.size:
            tops[i] = z[zi.max()]
    return tops


def envelope_extent_km(volume: GriddedVolume, cell: StormCell, azimuth_deg: float):
    """(dmin, dmax) signed distances where the envelope crosses the section line."""
    coords = np.asarray(cell.envelope.exterior.coords)
    xkm, ykm = sb._lonlat_to_km(volume, coords[:, 0], coords[:, 1])
    east = math.sin(math.radians(azimuth_deg))
    north = math.cos(math.radians(azimuth_deg))
    proj = (xkm - cell.seed_x / 1000.0) * east + (ykm - cell.seed_y / 1000.0) * north
    return float(np.min(proj)), float(np.max(proj))


# ---------------------------------------------------------------------------
# Panel render
# ---------------------------------------------------------------------------

def _panel_grid(section: Section, vexag: float):
    """A ``pyvista.ImageData`` of the slice in (distance, 0, height×vexag)."""
    import pyvista as pv

    nz, ndist = section.dbz.shape
    dd = float(section.dist_km[1] - section.dist_km[0]) if ndist > 1 else 1.0
    dz = float(section.z_km[1] - section.z_km[0]) if nz > 1 else 1.0
    grid = pv.ImageData()
    grid.dimensions = (ndist, 1, nz)
    grid.origin = (float(section.dist_km[0]), 0.0, float(section.z_km[0]) * vexag)
    grid.spacing = (dd, 1.0, dz * vexag)
    # (nz, ndist) C-order ravel == VTK order (x=distance fastest, then z).
    grid.point_data["dbz"] = np.nan_to_num(section.dbz, nan=sb.NAN_FILL).ravel(order="C")
    return grid


def build_panel(plotter, volume: GriddedVolume, cell: StormCell, section: Section,
                viewer: ViewerParams) -> None:
    """Render the cross-section panel + overlays into ``plotter`` (face-on)."""
    import pyvista as pv

    ve = XSECTION_VEXAG
    grid = _panel_grid(section, ve)
    cmap, _opacity, clim = sb.nws_color_opacity(viewer.vol_floor_dbz)
    # Threshold out below-floor cells so the empty panel stays black.
    panel = grid.threshold(viewer.vol_floor_dbz, scalars="dbz")
    try:
        if panel.n_points > 0:
            plotter.add_mesh(panel, scalars="dbz", cmap=cmap, clim=clim,
                             show_scalar_bar=False)
    except Exception as e:  # noqa: BLE001
        log.info("xsection.panel_skipped", reason=str(e).splitlines()[0])

    # Echo-top line over the panel.
    tops = echo_top_profile(section)
    finite = np.isfinite(tops)
    if finite.sum() >= 2:
        pts = np.column_stack([
            section.dist_km[finite], np.zeros(finite.sum()), tops[finite] * ve
        ])
        line = pv.lines_from_points(pts)
        plotter.add_mesh(line, color=(1.0, 1.0, 1.0), line_width=3.0)

    # Seed marker: vertical line at distance 0 from ground to echo top.
    top = max(cell.echo_top_km, 0.1) * ve
    plotter.add_mesh(pv.Line((0.0, 0.0, 0.0), (0.0, 0.0, top)),
                     color=(1.0, 1.0, 0.0), line_width=2.5)

    # Envelope horizontal extent: a bar near the ground.
    try:
        dmin, dmax = envelope_extent_km(volume, cell, section.azimuth_deg)
        z0 = float(section.z_km[0]) * ve
        plotter.add_mesh(pv.Line((dmin, 0.0, z0), (dmax, 0.0, z0)),
                         color=(0.2, 0.8, 1.0), line_width=4.0)
    except Exception as e:  # noqa: BLE001
        log.info("xsection.envelope_skipped", reason=str(e).splitlines()[0])

    try:
        plotter.enable_parallel_projection()
        plotter.view_xz()
        plotter.show_bounds(
            xtitle="distance along track (km)", ytitle="",
            ztitle=f"height (km x{ve:g})", color="gray",
        )
    except Exception as e:  # noqa: BLE001
        log.info("xsection.axes_skipped", reason=str(e).splitlines()[0])


def make_section(
    volume: GriddedVolume, cell: StormCell, results, viewer: ViewerParams,
    manual_deg: float | None = None,
) -> Section:
    """Azimuth → extracted :class:`Section` for ``cell`` (track heading default)."""
    az = section_azimuth(cell, results, viewer, manual_deg)
    half = sb.roi_window_km(volume, cell, pad_km=15.0)
    return extract_section(volume, cell, az, half)


def render_section(
    volume: GriddedVolume, cell: StormCell, results, viewer: ViewerParams,
    out_path: str, off_screen: bool = True, manual_deg: float | None = None,
) -> None:
    """Off-screen render of the cross-section to ``out_path`` (8B harness)."""
    import pyvista as pv

    section = make_section(volume, cell, results, viewer, manual_deg)
    plotter = pv.Plotter(off_screen=off_screen, window_size=(1024, 768))
    plotter.set_background("black")
    build_panel(plotter, volume, cell, section, viewer)
    plotter.screenshot(out_path)
    plotter.close()
