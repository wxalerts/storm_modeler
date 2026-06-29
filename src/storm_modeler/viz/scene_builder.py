"""Grid + cell → VTK/PyVista actors for the 3D model pane (Phase B1).

Pure rendering geometry: given a :class:`GriddedVolume` and the
:class:`StormCell` of interest, build the actors that make up the perspective
scene — a dBZ volume render, isosurface shells, the cell's envelope extruded
into a prism, and a labelled height marker at the seed. The builder owns no Qt
and performs no rendering itself: it adds actors to whatever ``pyvista.Plotter``
(interactive ``QtInteractor`` or off-screen) the caller supplies and returns
handles so layers can be toggled.

Scene coordinates are kilometres, centred on the radar: ``x`` east, ``y``
north, ``z`` height AGL. The z geometry is multiplied by ``vert_exag`` so storm
structure is readable; the labelled height marker always reports *true* km.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import structlog
from pyproj import Transformer

from ..data.radar_render import NWS_DBZ_TABLE
from ..detection.detection_v2 import StormCell
from ..models import PROJ_LOCK, GriddedVolume
from ..settings.resolver import ViewerParams

log = structlog.get_logger(__name__)

# dBZ range the NWS palette spans (used for the colour map clim).
DBZ_MIN = float(NWS_DBZ_TABLE[0][0])
DBZ_MAX = float(NWS_DBZ_TABLE[-1][0])
# Sentinel substituted for NaN (no echo). It sits below the palette so the
# volume transfer maps it to zero opacity; the colour map clim spans down to it
# so VTK does not clamp it up to the floor opacity (which would fog the domain).
NAN_FILL = DBZ_MIN - 35.0


# ---------------------------------------------------------------------------
# Palette helpers (shared NWS dBZ table → colour + opacity transfer functions)
# ---------------------------------------------------------------------------

def _nws_rgb(dbz: np.ndarray) -> np.ndarray:
    """Map dBZ values to (..., 3) float RGB in [0, 1] via the NWS table."""
    bounds = np.array([b for b, _ in NWS_DBZ_TABLE], dtype=np.float32)
    colors = np.array([c for _, c in NWS_DBZ_TABLE], dtype=np.float32) / 255.0
    idx = np.clip(np.searchsorted(bounds, dbz, side="right") - 1, 0, len(bounds) - 1)
    return colors[idx]


def nws_cmap(n: int = 64, clim: tuple[float, float] | None = None):
    """A matplotlib ``ListedColormap`` sampling the NWS dBZ table over ``clim``."""
    from matplotlib.colors import ListedColormap

    lo, hi = clim or (DBZ_MIN, DBZ_MAX)
    samples = np.linspace(lo, hi, n)
    return ListedColormap(_nws_rgb(samples))


def nws_color_opacity(
    floor_dbz: float, n: int = 128, clim: tuple[float, float] | None = None
):
    """Sampled (cmap, opacity, clim) for a PyVista volume transfer function.

    The clim spans from :data:`NAN_FILL` (the no-echo sentinel) up to the top of
    the dBZ range, so VTK never clamps empty cells up to a non-zero opacity. The
    ``opacity`` array is exactly 0 below ``floor_dbz`` and ramps to ~0.6 above,
    so cores read as solid while weak echo stays wispy and the empty domain is
    invisible.
    """
    lo, hi = clim or (NAN_FILL, DBZ_MAX)
    samples = np.linspace(lo, hi, n)
    frac = np.clip((samples - floor_dbz) / max(hi - floor_dbz, 1e-3), 0.0, 1.0)
    opacity = np.where(samples < floor_dbz, 0.0, 0.05 + 0.55 * frac)
    return nws_cmap(n, (lo, hi)), opacity.astype(np.float32), (lo, hi)


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def _lonlat_to_km(volume: GriddedVolume, lon, lat):
    """Project lon/lat onto the volume's radar-centred plane, in kilometres."""
    aeqd = (
        f"+proj=aeqd +lat_0={volume.lat0} +lon_0={volume.lon0} "
        "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
    )
    # Share the global PROJ lock: this runs on the GUI thread while a download
    # worker may be transforming concurrently (see models.PROJ_LOCK).
    with PROJ_LOCK:
        fwd = Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)
        xm, ym = fwd.transform(np.asarray(lon), np.asarray(lat))
    return np.asarray(xm) / 1000.0, np.asarray(ym) / 1000.0


def _dy_km(volume: GriddedVolume) -> float:
    if volume.y.size < 2:
        return volume.dx_km
    return float(np.mean(np.diff(volume.y))) / 1000.0


# ---------------------------------------------------------------------------
# Actor handles
# ---------------------------------------------------------------------------

LAYERS = ("volume", "isosurfaces", "envelope", "height", "other_cells")


@dataclass
class SceneActors:
    """Actor handles for the built scene, grouped by toggleable layer."""

    actors: dict[str, list] = field(default_factory=lambda: {k: [] for k in LAYERS})

    def add(self, layer: str, actor) -> None:
        if actor is not None:
            self.actors.setdefault(layer, []).append(actor)

    def set_visible(self, layer: str, on: bool) -> None:
        for a in self.actors.get(layer, []):
            try:
                a.SetVisibility(bool(on))
            except Exception:  # noqa: BLE001 - some handles aren't vtkProp
                pass

    def all_actors(self) -> list:
        return [a for group in self.actors.values() for a in group]


# ---------------------------------------------------------------------------
# Mesh builders
# ---------------------------------------------------------------------------

def volume_image(volume: GriddedVolume, vert_exag: float):
    """A ``pyvista.ImageData`` of the dBZ field in exaggerated-km coordinates."""
    import pyvista as pv

    refl = volume.reflectivity  # (nz, ny, nx)
    nz, ny, nx = refl.shape
    grid = pv.ImageData()
    grid.dimensions = (nx, ny, nz)
    grid.origin = (
        float(volume.x[0]) / 1000.0,
        float(volume.y[0]) / 1000.0,
        float(volume.z[0]) / 1000.0 * vert_exag,
    )
    grid.spacing = (volume.dx_km, _dy_km(volume), volume.dz_km * vert_exag)
    # VTK ImageData point order (x fastest, then y, then z) matches a C-order
    # ravel of refl[z, y, x]; NaN (no echo) sinks below the palette floor.
    grid.point_data["dbz"] = np.nan_to_num(refl, nan=NAN_FILL).ravel(order="C")
    return grid


def roi_window_km(volume: GriddedVolume, cell: StormCell, pad_km: float = 25.0) -> float:
    """Half-width (km) of a region of interest framing the cell + padding."""
    coords = np.asarray(cell.envelope.exterior.coords)
    xkm, ykm = _lonlat_to_km(volume, coords[:, 0], coords[:, 1])
    sx, sy = cell.seed_x / 1000.0, cell.seed_y / 1000.0
    reach = float(np.max(np.abs([xkm - sx, ykm - sy]))) if xkm.size else 0.0
    return max(reach, 8.0) + pad_km


def crop_to_cell(grid, volume: GriddedVolume, cell: StormCell, half_km: float):
    """Extract the voxel subset of ``grid`` within ``half_km`` of the seed (x/y).

    Keeps the full vertical column. Origin/spacing are preserved so the cropped
    grid stays in the same exaggerated-km coordinate frame as the prism/marker.
    """
    dx, dy = volume.dx_km, _dy_km(volume)
    ox, oy = float(volume.x[0]) / 1000.0, float(volume.y[0]) / 1000.0
    sx, sy = cell.seed_x / 1000.0, cell.seed_y / 1000.0
    nx, ny, nz = grid.dimensions
    i0 = max(0, int((sx - half_km - ox) / dx))
    i1 = min(nx - 1, int((sx + half_km - ox) / dx))
    j0 = max(0, int((sy - half_km - oy) / dy))
    j1 = min(ny - 1, int((sy + half_km - oy) / dy))
    if i1 <= i0 or j1 <= j0:
        return grid
    return grid.extract_subset((i0, i1, j0, j1, 0, nz - 1))


def envelope_prism(volume: GriddedVolume, cell: StormCell, vert_exag: float):
    """Extrude the cell's footprint from ground to its echo top (a prism)."""
    import pyvista as pv

    coords = np.asarray(cell.envelope.exterior.coords)
    if coords.shape[0] >= 2 and np.allclose(coords[0], coords[-1]):
        coords = coords[:-1]  # drop the closing duplicate
    xkm, ykm = _lonlat_to_km(volume, coords[:, 0], coords[:, 1])
    n = xkm.size
    if n < 3:
        return None
    pts = np.column_stack([xkm, ykm, np.zeros(n)])
    base = pv.PolyData(pts, faces=np.hstack([[n], np.arange(n)]))
    top_km = max(cell.echo_top_km, 0.1) * vert_exag
    return base.extrude((0.0, 0.0, top_km), capping=True)


def height_line(cell: StormCell, vert_exag: float):
    """A vertical line at the seed from ground to the echo top."""
    import pyvista as pv

    x, y = cell.seed_x / 1000.0, cell.seed_y / 1000.0
    top = max(cell.echo_top_km, 0.1) * vert_exag
    return pv.Line((x, y, 0.0), (x, y, top))


# ---------------------------------------------------------------------------
# Scene assembly
# ---------------------------------------------------------------------------

def build_scene(
    plotter,
    volume: GriddedVolume,
    cell: StormCell,
    viewer: ViewerParams | None = None,
    other_cells: list[StormCell] | None = None,
    toggles: dict[str, bool] | None = None,
) -> SceneActors:
    """Build the full 3D scene for ``cell`` into ``plotter``; return handles.

    ``toggles`` (per :data:`LAYERS`) lets the caller pre-hide layers; the
    returned :class:`SceneActors` flips visibility live afterwards.
    """
    import pyvista as pv

    viewer = viewer or ViewerParams()
    toggles = toggles or {}
    other_cells = other_cells or []
    ve = viewer.vert_exag
    out = SceneActors()

    # --- volume render --------------------------------------------------
    grid = crop_to_cell(volume_image(volume, ve), volume, cell, roi_window_km(volume, cell))
    cmap, opacity, clim = nws_color_opacity(viewer.vol_floor_dbz)
    try:
        vol_actor = plotter.add_volume(
            grid,
            scalars="dbz",
            clim=clim,
            cmap=cmap,
            opacity=opacity,
            shade=False,
            show_scalar_bar=False,
        )
        out.add("volume", vol_actor)
    except Exception as e:  # noqa: BLE001 - headless GL may reject volume mapper
        log.info("scene.volume_skipped", reason=str(e).splitlines()[0])

    # --- isosurface shells ----------------------------------------------
    levels = [lv for lv in viewer.iso_levels_dbz]
    if levels:
        try:
            iso = grid.contour(levels, scalars="dbz")
            if iso.n_points > 0:
                iso_actor = plotter.add_mesh(
                    iso, scalars="dbz", cmap=cmap,
                    clim=clim, opacity=0.45, show_scalar_bar=False,
                    smooth_shading=True,
                )
                out.add("isosurfaces", iso_actor)
        except Exception as e:  # noqa: BLE001
            log.info("scene.iso_skipped", reason=str(e).splitlines()[0])

    # --- envelope prism (selected cell emphasised) ----------------------
    prism = envelope_prism(volume, cell, ve)
    if prism is not None:
        out.add("envelope", plotter.add_mesh(
            prism, color=(1.0, 1.0, 0.0), opacity=0.18, show_scalar_bar=False,
        ))

    # --- other cells (faint context) ------------------------------------
    for oc in other_cells:
        if oc is cell or oc.cell_id == cell.cell_id:
            continue
        op = envelope_prism(volume, oc, ve)
        if op is not None:
            out.add("other_cells", plotter.add_mesh(
                op, color=(0.5, 0.5, 0.55), opacity=0.06, show_scalar_bar=False,
            ))

    # --- height marker + label ------------------------------------------
    line = height_line(cell, ve)
    out.add("height", plotter.add_mesh(line, color=(1.0, 1.0, 1.0), line_width=4.0))
    try:
        top_pt = np.array([[cell.seed_x / 1000.0, cell.seed_y / 1000.0,
                            max(cell.echo_top_km, 0.1) * ve]])
        label = f"echo top {cell.echo_top_km:.1f} km\ndepth {cell.depth_km:.1f} km"
        out.add("height", plotter.add_point_labels(
            top_pt, [label], font_size=11, text_color="white",
            shape_opacity=0.3, show_points=True, point_size=8,
            point_color="white", always_visible=True,
        ))
    except Exception as e:  # noqa: BLE001
        log.info("scene.label_skipped", reason=str(e).splitlines()[0])

    # --- apply initial toggles ------------------------------------------
    for layer in LAYERS:
        if layer in toggles and not toggles[layer]:
            out.set_visible(layer, False)

    return out
