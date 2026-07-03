"""Storm-relative Vrot correction (detection.srm) — the one-sided regression."""

from __future__ import annotations

import math

from storm_modeler.detection.srm import correct_vrot


def test_two_signed_couplet_zero_motion_is_plain_rule():
    vrot, vmax_sr, vmin_sr = correct_vrot(25.0, -25.0, 90.0, 0.0, 0.0)
    assert vrot == 25.0
    assert vmax_sr == 25.0 and vmin_sr == -25.0


def test_one_sided_couplet_recovers_through_matching_motion():
    """The regression this helper exists for.

    A ±25 m/s couplet west of the radar (az 270°) seen through 30 m/s of
    eastward flow reads −5/−55 — entirely one-sided. The uncorrected rule
    degrades it through the weak-signal branch (27.5, not 25); shifting into
    the storm-relative frame recovers the true 25 m/s rotation.
    """
    v_max, v_min = -5.0, -55.0  # ±25 couplet + (u=30) projected at az 270°

    old_vrot, _, _ = correct_vrot(v_max, v_min, 270.0, 0.0, 0.0)
    assert old_vrot != 25.0
    assert old_vrot == max(abs(v_max), abs(v_min)) / 2.0  # one-sided branch

    vrot, vmax_sr, vmin_sr = correct_vrot(v_max, v_min, 270.0, 30.0, 0.0)
    assert abs(vrot - 25.0) < 1e-9
    assert abs(vmax_sr - 25.0) < 1e-9 and abs(vmin_sr + 25.0) < 1e-9


def test_shift_at_az_zero_uses_only_v_component():
    # Due north of the radar the radial is the +y axis: only v_ms projects.
    _, vmax_sr, vmin_sr = correct_vrot(10.0, -10.0, 0.0, 100.0, 7.0)
    assert abs(vmax_sr - (10.0 - 7.0)) < 1e-9
    assert abs(vmin_sr - (-10.0 - 7.0)) < 1e-9


def test_shift_at_az_ninety_uses_only_u_component():
    # Due east the radial is the +x axis: only u_ms projects.
    _, vmax_sr, vmin_sr = correct_vrot(10.0, -10.0, 90.0, 7.0, 100.0)
    assert abs(vmax_sr - (10.0 - 7.0)) < 1e-9
    assert abs(vmin_sr - (-10.0 - 7.0)) < 1e-9


def test_shift_is_radial_projection_at_arbitrary_azimuth():
    az, u, v = 213.0, 11.0, -4.0
    expected = u * math.sin(math.radians(az)) + v * math.cos(math.radians(az))
    _, vmax_sr, _ = correct_vrot(0.0, -1.0, az, u, v)
    assert abs((0.0 - vmax_sr) - expected) < 1e-12
