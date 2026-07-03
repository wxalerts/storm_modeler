"""Unit tests for the map's velocity rendering (AWIPS 8-bit Vel.cmap + RF)."""

from __future__ import annotations

import numpy as np

from storm_modeler.data.radar_render import (
    NWS_VEL_TABLE,
    VEL_RF_INDEX,
    product_lonlat_image,
    vel_to_rgba,
)
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_storm_volume

RF_PURPLE = (0x7F, 0x00, 0xCF)


def test_vel_table_structure():
    assert NWS_VEL_TABLE.shape == (256, 3)
    assert tuple(NWS_VEL_TABLE[0]) == (0, 0, 0)  # no-data entry
    assert tuple(NWS_VEL_TABLE[VEL_RF_INDEX]) == RF_PURPLE  # range folded
    assert tuple(NWS_VEL_TABLE[2]) == (0xFF, 0x00, 0x84)  # inbound magenta extreme
    assert tuple(NWS_VEL_TABLE[255]) == (0xFF, 0xFF, 0xFF)  # outbound white extreme
    # Center of the ramp is the desaturated near-gray zero, not green/red.
    center = NWS_VEL_TABLE[128].astype(int)
    assert center.max() - center.min() < 40


def test_vel_to_rgba_mapping():
    vel = np.array([[np.nan, -32.0, 0.0, 32.0, -100.0, 100.0]], dtype=np.float32)
    rgba = vel_to_rgba(vel, vmin=-32.0, vmax=32.0)
    assert rgba[0, 0, 3] == 0  # NaN → transparent
    assert tuple(rgba[0, 1, :3]) == tuple(NWS_VEL_TABLE[2])  # vmin → magenta end
    assert tuple(rgba[0, 3, :3]) == (255, 255, 255)  # vmax → white end
    # Out-of-range clamps to the extremes rather than wrapping.
    assert tuple(rgba[0, 4, :3]) == tuple(NWS_VEL_TABLE[2])
    assert tuple(rgba[0, 5, :3]) == (255, 255, 255)
    # Zero lands on the gray center of the ramp (index 128/129).
    assert tuple(rgba[0, 2, :3]) in (tuple(NWS_VEL_TABLE[128]), tuple(NWS_VEL_TABLE[129]))
    assert (rgba[0, 1:, 3] == 255).all()


def test_vel_to_rgba_range_folded_wins():
    vel = np.array([[10.0, np.nan]], dtype=np.float32)
    rf = np.array([[True, True]])
    rgba = vel_to_rgba(vel, range_folded=rf)
    assert tuple(rgba[0, 0, :3]) == RF_PURPLE
    assert tuple(rgba[0, 1, :3]) == RF_PURPLE
    assert (rgba[0, :, 3] == 255).all()


def _velocity_volume(with_rf: bool):
    s = get_site("KFWS")
    vol = make_storm_volume(
        "KFWS", s.lat, s.lon, "2024-05-25T17:42:00Z",
        core_lon=s.lon + 0.1, core_lat=s.lat + 0.1,
    )
    ny, nx = vol.reflectivity.shape[1:]
    vel = np.full((ny, nx), np.nan, dtype=np.float32)
    vel[: ny // 2] = 15.0  # north half outbound
    vel[ny // 2:] = -15.0  # south half inbound
    vol.products["velocity"] = vel
    if with_rf:
        rf = np.zeros((ny, nx), dtype=np.float32)
        rf[ny // 3: ny // 2, nx // 3: nx // 2] = 1.0
        vol.products["velocity"] = np.where(rf >= 0.5, np.nan, vel)
        vol.products["range_folded"] = rf
    return vol


def test_product_image_paints_range_folded_purple():
    vol = _velocity_volume(with_rf=True)
    rgba, _bounds = product_lonlat_image(vol, "velocity", width=256, height=256)
    is_rf = (
        (rgba[..., 0] == RF_PURPLE[0])
        & (rgba[..., 1] == RF_PURPLE[1])
        & (rgba[..., 2] == RF_PURPLE[2])
        & (rgba[..., 3] == 255)
    )
    assert is_rf.any(), "RF block should render as the table's purple"
    # Non-RF velocity still renders opaque, non-purple pixels.
    opaque = rgba[..., 3] == 255
    assert (opaque & ~is_rf).any()


def test_product_image_without_rf_layer_still_renders():
    vol = _velocity_volume(with_rf=False)
    rgba, _bounds = product_lonlat_image(vol, "velocity", width=128, height=128)
    assert (rgba[..., 3] == 255).any()
