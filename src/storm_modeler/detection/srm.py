"""Storm-relative Vrot correction — pure math, wired to nothing yet.

A velocity couplet's rotational velocity is read from its two radial-velocity
extrema: ``(|v_max| + |v_min|) / 2`` when they straddle zero, else a one-sided
"weak signal" ``max(|v|) / 2``. That rule is frame-sensitive: strong
ground-relative background flow can push a genuine couplet entirely one-sided
(e.g. a ±25 m/s couplet seen through 30 m/s of along-radial flow reads
−55/−5), silently degrading Vrot through the one-sided branch even though the
rotation itself is unchanged.

:func:`correct_vrot` removes the frame dependence: shift both extrema into the
storm-relative frame via the scalar projection of storm motion onto the
couplet's center azimuth (over a couplet's few-degree azimuth span the
projection is effectively constant), then apply the same two-signed /
one-sided rule.

This is a helper for a future couplet-association pass; nothing imports it
yet, deliberately — there is no couplet detection in this repo today (the
"SCIT" package, ``detection_v2``, is reflectivity-based cell identification).
"""

from __future__ import annotations

import math


def correct_vrot(
    v_max: float,
    v_min: float,
    center_az_deg: float,
    u_ms: float,
    v_ms: float,
) -> tuple[float, float, float]:
    """Return ``(vrot_ms, v_max_sr, v_min_sr)`` for a couplet's extrema.

    ``v_max``/``v_min`` are the couplet's ground-relative radial-velocity
    extrema (m/s, positive away from the radar); ``center_az_deg`` the
    couplet's center azimuth (degrees from north, clockwise, as seen from the
    radar); ``(u_ms, v_ms)`` the storm motion (m/s east/north). Both extrema
    are shifted into the storm-relative frame by the motion's projection onto
    the radial at that azimuth, then Vrot follows the standard rule:
    ``(|v_max_sr| + |v_min_sr|) / 2`` when the shifted extrema straddle zero,
    else the one-sided ``max(|v|) / 2`` weak-signal branch. Zero motion
    reproduces the uncorrected behavior exactly.
    """
    shift = (
        u_ms * math.sin(math.radians(center_az_deg))
        + v_ms * math.cos(math.radians(center_az_deg))
    )
    v_max_sr = float(v_max) - shift
    v_min_sr = float(v_min) - shift
    if v_max_sr > 0.0 > v_min_sr:
        vrot = (abs(v_max_sr) + abs(v_min_sr)) / 2.0
    else:
        vrot = max(abs(v_max_sr), abs(v_min_sr)) / 2.0
    return vrot, v_max_sr, v_min_sr
