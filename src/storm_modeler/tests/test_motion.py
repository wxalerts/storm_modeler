"""Storm-motion helper + SRM frame resolution (pure logic, no GL / no Qt)."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from shapely.geometry import Point

from storm_modeler.detection.detection_v2 import StormCell
from storm_modeler.viz import motion


def _cell(minute: int, sx: float, sy: float, track_id: int = 7) -> StormCell:
    t = datetime(2024, 5, 25, 12, minute, tzinfo=timezone.utc)
    env = Point(-97.3, 32.6).buffer(0.05)
    return StormCell(
        cell_id=1, site="KFWS", valid_time=t, seed_lon=-97.3, seed_lat=32.6,
        seed_x=sx, seed_y=sy, max_dbz=55.0, area_km2=20.0, echo_top_km=10.0,
        base_km=1.0, depth_km=9.0, n_levels=12, envelope=env, track_id=track_id,
    )


def _results(*cell_groups):
    return [SimpleNamespace(cells=list(g)) for g in cell_groups]


def test_track_motion_east_moving_seeds():
    c0 = _cell(0, 0.0, 0.0)
    c1 = _cell(6, 5000.0, 0.0)  # 5 km east in 6 minutes
    uv = motion.track_motion_uv(c1, _results([c0], [c1]))
    assert uv is not None
    u, v = uv
    assert abs(u - 5000.0 / 360.0) < 1e-9  # ≈13.9 m/s east
    assert abs(v) < 1e-9


def test_track_motion_single_seed_is_none():
    c = _cell(0, 0.0, 0.0)
    assert motion.track_motion_uv(c, _results([c])) is None


def test_track_motion_untracked_cell_is_none():
    c = _cell(0, 0.0, 0.0, track_id=-1)
    assert motion.track_motion_uv(c, _results([c], [c])) is None


def test_mean_motion_averages_live_tracks():
    a0, a1 = _cell(0, 0.0, 0.0, track_id=1), _cell(6, 3600.0, 0.0, track_id=1)
    b0, b1 = _cell(0, 0.0, 0.0, track_id=2), _cell(6, 0.0, 7200.0, track_id=2)
    results = _results([a0, b0], [a1, b1])
    uv = motion.mean_motion_uv([a1, b1], results)
    assert uv is not None
    assert abs(uv[0] - 5.0) < 1e-9   # (10 + 0) / 2
    assert abs(uv[1] - 10.0) < 1e-9  # (0 + 20) / 2


def test_speed_dir_round_trip_meteorological():
    # 30 kt out of the southwest (240°) moves the storm northeast: u, v > 0.
    u, v = motion.uv_from_speed_dir(30.0, 240.0)
    assert u > 0 and v > 0
    speed, direction = motion.speed_dir_from_uv(u, v)
    assert abs(speed - 30.0) < 1e-9
    assert abs(direction - 240.0) < 1e-9
    assert motion.speed_dir_from_uv(0.0, 0.0) == (0.0, 0.0)


def test_resolve_motion_prefers_manual_when_configured():
    uv, used = motion.resolve_motion("manual", None, [], [], manual_uv=(3.0, 4.0))
    assert uv == (3.0, 4.0) and used == "manual"


def test_resolve_motion_selected_track_then_mean_then_zero():
    c0 = _cell(0, 0.0, 0.0)
    c1 = _cell(6, 5000.0, 0.0)
    results = _results([c0], [c1])

    uv, used = motion.resolve_motion("selected_track", c1, [c1], results)
    assert used == "selected_track" and uv[0] > 0

    # Selected cell untracked -> falls back to the volume's mean track motion.
    loner = _cell(6, 0.0, 0.0, track_id=-1)
    uv, used = motion.resolve_motion("selected_track", loner, [loner, c1], results)
    assert used == "mean_tracks" and uv[0] > 0

    # Nothing derivable -> (0, 0), SRM degrades to base velocity.
    uv, used = motion.resolve_motion("selected_track", loner, [loner],
                                     _results([loner]))
    assert uv == (0.0, 0.0) and used == "none"


def test_resolve_motion_mean_source_skips_selected_track():
    c0 = _cell(0, 0.0, 0.0)
    c1 = _cell(6, 5000.0, 0.0)
    results = _results([c0], [c1])
    uv, used = motion.resolve_motion("mean_tracks", c1, [c1], results)
    assert used == "mean_tracks"
    assert abs(uv[0] - 5000.0 / 360.0) < 1e-9
