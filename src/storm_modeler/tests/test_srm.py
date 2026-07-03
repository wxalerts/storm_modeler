"""Storm-relative velocity derivation (pure numpy, no Qt/GL)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from storm_modeler.data.radar_render import derive_srm, ensure_srm_layer
from storm_modeler.models import GriddedVolume

T = datetime(2024, 5, 25, 12, 0, tzinfo=timezone.utc)


def _volume(vel2d: np.ndarray) -> GriddedVolume:
    """A minimal radar-centred volume carrying ``vel2d`` as its velocity layer.

    The layer is injected after construction so it keeps its float64 dtype
    (``__post_init__`` casts constructor-passed products to float32).
    """
    ny, nx = vel2d.shape
    x = np.linspace(-50000.0, 50000.0, nx)
    y = np.linspace(-50000.0, 50000.0, ny)
    z = np.array([500.0, 1000.0])
    refl = np.full((z.size, ny, nx), np.nan, dtype=np.float32)
    vol = GriddedVolume("KFWS", T, refl, x, y, z, 32.57, -97.30)
    vol.products["velocity"] = vel2d
    return vol


def _radial_projection(vol: GriddedVolume, u: float, v: float) -> np.ndarray:
    xx, yy = np.meshgrid(vol.x, vol.y)
    r = np.maximum(np.hypot(xx, yy), 1.0)
    return (u * xx + v * yy) / r


def test_uniform_flow_cancels_to_zero():
    u, v = 12.0, -7.0
    vol = _volume(np.zeros((101, 101)))
    vol.products["velocity"] = _radial_projection(vol, u, v)
    srm = derive_srm(vol, u, v)
    assert np.allclose(srm, 0.0, atol=1e-6)


def test_zero_motion_is_identity():
    rng = np.linspace(-30.0, 30.0, 101)
    field = np.add.outer(rng, rng)  # arbitrary smooth field
    field[3, 4] = np.nan
    vol = _volume(field)
    srm = derive_srm(vol, 0.0, 0.0)
    assert np.allclose(srm, field, atol=0.0, equal_nan=True)


def test_nan_gates_stay_nan():
    field = np.full((51, 51), 10.0)
    field[10:20, 30:40] = np.nan
    vol = _volume(field)
    srm = derive_srm(vol, 15.0, 5.0)
    assert np.isnan(srm[10:20, 30:40]).all()
    assert np.isfinite(np.delete(srm.ravel(), np.flatnonzero(np.isnan(field)))).all()


def _rankine_radial_field(vol: GriddedVolume, x0: float, y0: float,
                          vmax: float, radius: float,
                          u_t: float = 0.0, v_t: float = 0.0) -> np.ndarray:
    """Radial-velocity signature of a Rankine vortex translating at (u_t, v_t)."""
    xx, yy = np.meshgrid(vol.x, vol.y)
    dx, dy = xx - x0, yy - y0
    s = np.maximum(np.hypot(dx, dy), 1.0)
    vt = vmax * np.where(s < radius, s / radius, radius / s)
    uw = -vt * dy / s + u_t  # counterclockwise tangential wind + translation
    vw = vt * dx / s + v_t
    r = np.maximum(np.hypot(xx, yy), 1.0)
    return (uw * xx + vw * yy) / r


def test_translated_rankine_couplet_recovers_untranslated_signature():
    vol = _volume(np.zeros((101, 101)))
    still = _rankine_radial_field(vol, 20000.0, 10000.0, 25.0, 4000.0)
    moving = _rankine_radial_field(vol, 20000.0, 10000.0, 25.0, 4000.0,
                                   u_t=30.0, v_t=0.0)
    vol.products["velocity"] = moving
    srm = derive_srm(vol, 30.0, 0.0)
    assert np.allclose(srm, still, atol=1e-6)
    # The ground-relative field is one-sided over the couplet; SRM restores
    # the symmetric ±25 m/s signature.
    assert still.min() < -20.0 and still.max() > 20.0


def test_ensure_srm_layer_caches_until_motion_changes():
    vol = _volume(np.zeros((51, 51)))
    vol.products["velocity"] = _radial_projection(vol, 10.0, 0.0)
    a = ensure_srm_layer(vol, 10.0, 0.0)
    assert a is vol.products["storm_relative_velocity"]
    # Sub-threshold nudge: cached layer is reused (identity, no recompute).
    b = ensure_srm_layer(vol, 10.05, 0.05)
    assert b is a
    # A real change recomputes.
    c = ensure_srm_layer(vol, 15.0, 0.0)
    assert c is not a
    assert not np.allclose(c, a)


def test_ensure_srm_layer_without_velocity_returns_none():
    ny = nx = 11
    refl = np.full((2, ny, nx), np.nan, dtype=np.float32)
    vol = GriddedVolume("KFWS", T, refl,
                        np.linspace(-5000, 5000, nx), np.linspace(-5000, 5000, ny),
                        np.array([500.0, 1000.0]), 32.57, -97.30)
    assert ensure_srm_layer(vol, 10.0, 0.0) is None
    assert "storm_relative_velocity" not in vol.products
