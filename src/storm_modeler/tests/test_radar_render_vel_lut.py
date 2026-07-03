"""AWIPS 8-bit velocity LUT asset + colorize mapping (Phase 1 acceptance)."""

from __future__ import annotations

import numpy as np

from storm_modeler.config import PACKAGE_ROOT
from storm_modeler.data.radar_render import (
    AWIPS_VEL_LUT,
    NWS_VEL_TABLE,
    _colorize,
    vel_to_rgba,
)


def test_lut_asset_loads_with_expected_shape():
    lut = np.load(PACKAGE_ROOT / "assets" / "awips_vel_lut.npz")["rgba_float"]
    assert lut.shape == (256, 4)
    assert lut.dtype == np.float64
    assert 0.0 <= lut.min() and lut.max() <= 1.0
    assert np.array_equal(lut, AWIPS_VEL_LUT)


def test_index_zero_is_transparent_no_data():
    assert AWIPS_VEL_LUT[0, 3] == 0.0
    # Every other index is opaque data (or RF).
    assert (AWIPS_VEL_LUT[1:, 3] == 1.0).all()


def test_all_nan_field_colorizes_fully_transparent():
    field = np.full((8, 8), np.nan, dtype=np.float32)
    rgba = _colorize(field, "velocity")
    assert (rgba[..., 3] == 0).all()


def test_zero_velocity_maps_to_gray_center():
    rgba = vel_to_rgba(np.array([[0.0]], dtype=np.float32))
    r, g, b = (int(v) for v in rgba[0, 0, :3])
    spread = max(r, g, b) - min(r, g, b)
    assert spread < 25, f"center should be desaturated gray, got {(r, g, b)}"
    assert rgba[0, 0, 3] == 255


def test_window_endpoints_map_to_ramp_ends():
    rgba = vel_to_rgba(np.array([[-32.0, 32.0]], dtype=np.float32),
                       vmin=-32.0, vmax=32.0)
    assert tuple(rgba[0, 0, :3]) == tuple(NWS_VEL_TABLE[2])    # first ramp index
    assert tuple(rgba[0, 1, :3]) == tuple(NWS_VEL_TABLE[255])  # last ramp index


def test_out_of_window_values_clip_not_wrap():
    rgba = vel_to_rgba(np.array([[-500.0, 500.0]], dtype=np.float32),
                       vmin=-32.0, vmax=32.0)
    assert tuple(rgba[0, 0, :3]) == tuple(NWS_VEL_TABLE[2])
    assert tuple(rgba[0, 1, :3]) == tuple(NWS_VEL_TABLE[255])
    assert (rgba[0, :, 3] == 255).all()


def test_range_folded_mask_takes_index_one_purple():
    field = np.array([[5.0, np.nan]], dtype=np.float32)
    rf = np.array([[True, True]])
    rgba = _colorize(field, "velocity", range_folded=rf)
    assert tuple(rgba[0, 0, :3]) == (0x7F, 0x00, 0xCF)
    assert tuple(rgba[0, 1, :3]) == (0x7F, 0x00, 0xCF)
    assert (rgba[0, :, 3] == 255).all()
