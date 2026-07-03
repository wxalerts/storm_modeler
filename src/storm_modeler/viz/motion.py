"""Storm-motion vectors from track history (pure geometry, no GUI, no GL).

The seed positions SCIT records are metres east/north of the radar — a frame
that is constant across a warning's volumes — so a track segment's
displacement over its time delta *is* the storm-motion vector. The
cross-section azimuth (``viz.xsection.section_azimuth``, heading only) and the
SRM display frame (full (u, v), ``data.radar_render.ensure_srm_layer``) share
the same segment walk here rather than each re-deriving it.

Directions follow the meteorological convention throughout: ``dir_deg`` is the
bearing the storm moves FROM (240° @ 30 kt = a southwest flow moving
northeast), matching how NWS storm motion is read.
"""

from __future__ import annotations

import math
from datetime import datetime

MS_PER_KT = 0.514444

#: Displacement (m) below which a track segment carries no usable direction.
MIN_SEGMENT_M = 1.0


def track_seeds(cell, results) -> list[tuple[datetime, float, float]]:
    """Ordered (valid_time, seed_x, seed_y) for ``cell``'s track across volumes."""
    tid = cell.track_id
    pts: list[tuple] = []
    for res in results:
        for c in res.cells:
            if tid >= 0 and c.track_id == tid:
                pts.append((c.valid_time, c.seed_x, c.seed_y))
    pts.sort(key=lambda p: p[0])
    return pts


def track_segment(cell, results) -> tuple[float, float, float] | None:
    """(dx_m, dy_m, dt_s) of the track segment bracketing ``cell``'s time.

    Prefers the segment leaving the cell's volume, falling back to the one
    arriving at it; ``None`` when the track has no segment with more than
    :data:`MIN_SEGMENT_M` of displacement (single seed, stationary echo).
    """
    pts = track_seeds(cell, results)
    idx = next((i for i, p in enumerate(pts) if p[0] == cell.valid_time), -1)
    for a, b in ((idx, idx + 1), (idx - 1, idx)):
        if 0 <= a < len(pts) and 0 <= b < len(pts) and a != b:
            dx = pts[b][1] - pts[a][1]  # east
            dy = pts[b][2] - pts[a][2]  # north
            if math.hypot(dx, dy) > MIN_SEGMENT_M:
                dt = (pts[b][0] - pts[a][0]).total_seconds()
                return dx, dy, dt
    return None


def track_motion_uv(cell, results) -> tuple[float, float] | None:
    """Storm motion (u_ms east, v_ms north) from the cell's track segment."""
    seg = track_segment(cell, results)
    if seg is None:
        return None
    dx, dy, dt = seg
    if dt <= 0:
        return None
    return dx / dt, dy / dt


def mean_motion_uv(cells, results) -> tuple[float, float] | None:
    """Mean (u, v) over the tracked cells' motions (``None`` if none yields one).

    ``cells`` is typically one volume's detections; untracked cells
    (``track_id == -1``) have no history and are skipped.
    """
    vecs = [track_motion_uv(c, results) for c in cells if c.track_id >= 0]
    vecs = [v for v in vecs if v is not None]
    if not vecs:
        return None
    return (
        sum(v[0] for v in vecs) / len(vecs),
        sum(v[1] for v in vecs) / len(vecs),
    )


def uv_from_speed_dir(speed_kt: float, dir_deg: float) -> tuple[float, float]:
    """Motion components (m/s east, north) from meteorological speed/direction."""
    ms = float(speed_kt) * MS_PER_KT
    rad = math.radians(float(dir_deg))
    return -ms * math.sin(rad), -ms * math.cos(rad)


def speed_dir_from_uv(u_ms: float, v_ms: float) -> tuple[float, float]:
    """(speed_kt, dir_deg FROM) of a motion vector; dir 0 for a calm vector."""
    speed_kt = math.hypot(u_ms, v_ms) / MS_PER_KT
    if speed_kt < 1e-9:
        return 0.0, 0.0
    dir_deg = math.degrees(math.atan2(-u_ms, -v_ms)) % 360.0
    return speed_kt, dir_deg


def resolve_motion(
    source: str,
    cell,
    cells,
    results,
    manual_uv: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], str]:
    """Resolve the SRM frame per the configured source, with ordered fallbacks.

    ``source`` is the ``srm_motion_source`` setting. Returns ``((u, v), used)``
    where ``used`` names the level that actually supplied the vector:
    ``manual`` → ``selected_track`` → ``mean_tracks`` → ``none`` ((0, 0): SRM
    degrades to base velocity). Pure — the caller logs fallbacks.
    """
    if source == "manual" and manual_uv is not None:
        return (float(manual_uv[0]), float(manual_uv[1])), "manual"
    if source != "mean_tracks" and cell is not None:
        uv = track_motion_uv(cell, results)
        if uv is not None:
            return uv, "selected_track"
    uv = mean_motion_uv(cells or [], results)
    if uv is not None:
        return uv, "mean_tracks"
    return (0.0, 0.0), "none"
