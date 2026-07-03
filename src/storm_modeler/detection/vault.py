"""Vault detection — radar echo tower vs the HRRR 0 °C freezing level.

A strong updraft lofts precipitation-sized hydrometeors far above the freezing
level, leaving a high-reflectivity tower (the storm's *vault*) standing above
the 0 °C surface. Measuring how far the ``>= vault_dbz`` echo extends above the
HRRR freezing level is the classic strong-updraft / severe-hail proxy (Donavon
& Jungbluth 2007), and it is what now derives each cell's ``overshooting_top``
flag — the GOES ABI anvil-depth OT flag proved unreliable and was removed.

Pure given the volume, its cells, and one :class:`FreezingLevelGrid`: takes an
explicit :class:`VaultParams`, reads no globals, touches no pyproj (the cell's
seed x/y in grid metres and the volume's grid axes are all it needs). The
caller pairs the volume to the freezing-level grid nearest in time before
calling. Mutates the cells in place and returns them.
"""

from __future__ import annotations

import math

import numpy as np

from ..models import FreezingLevelGrid, GriddedVolume
from ..settings.resolver import VaultParams
from .detection_v2.types import StormCell

#: Minimum search radius (km) around the seed for the cell's tower columns —
#: keeps a tiny footprint from missing a slightly tilted core.
_MIN_RADIUS_KM = 3.0


def detect_vault(
    volume: GriddedVolume,
    cells: list[StormCell],
    flevel: FreezingLevelGrid,
    site_elevation_m: float,
    params: VaultParams | None = None,
) -> list[StormCell]:
    """Annotate each cell with its vault metrics against the 0 °C level.

    For every cell, the columns within the cell's equivalent radius of its seed
    are scanned for the tallest ``>= vault_dbz`` echo; that tower top (grid
    z is AGL, so ``+ site_elevation_m`` puts it on MSL like the HRRR heights)
    is compared with the freezing level sampled at the seed. Sets
    ``freezing_level_km`` / ``vault_top_km`` / ``vault_depth_km`` and derives
    ``overshooting_top`` (depth at/above ``vault_min_depth_km``). Cells with no
    echo at the vault threshold keep ``vault_top_km is None`` and a False flag.
    """
    params = params or VaultParams()
    if not cells:
        return cells

    filled = np.nan_to_num(volume.reflectivity, nan=-9999.0)  # (nz, ny, nx)
    xx, yy = np.meshgrid(volume.x, volume.y)  # (ny, nx) grid metres
    z_msl_km = (volume.z + float(site_elevation_m)) / 1000.0  # (nz,)

    for c in cells:
        fl_m = flevel.height_at(c.seed_lon, c.seed_lat)
        if not math.isfinite(fl_m):
            fl_m = flevel.mean_height_m()  # seed off the raster: window mean
        if not math.isfinite(fl_m):
            continue  # empty grid — leave the cell unannotated
        c.freezing_level_km = fl_m / 1000.0

        r_m = max(_MIN_RADIUS_KM, math.sqrt(c.area_km2 / math.pi)) * 1000.0
        cols = np.hypot(xx - c.seed_x, yy - c.seed_y) <= r_m  # (ny, nx)
        tower = (filled[:, cols] >= params.vault_dbz).any(axis=1)  # (nz,)
        if tower.any():
            k_top = int(np.nonzero(tower)[0].max())
            c.vault_top_km = float(z_msl_km[k_top])
            c.vault_depth_km = c.vault_top_km - c.freezing_level_km
            c.overshooting_top = c.vault_depth_km >= params.vault_min_depth_km
        else:
            c.vault_top_km = None
            c.vault_depth_km = None
            c.overshooting_top = False
    return cells
