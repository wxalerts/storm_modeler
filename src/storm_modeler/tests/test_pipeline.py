"""End-to-end (offline, no DB) pipeline tests over generated replay cases."""

from __future__ import annotations

from datetime import datetime, timezone

from shapely.geometry import Polygon

from storm_modeler.data.satellite import FixtureSceneSource
from storm_modeler.data.sites import get_site
from storm_modeler.data.synthetic import make_cold_scene, make_storm_volume
from storm_modeler.detection.detection_v2 import run as scit_run
from storm_modeler.models import Warning
from storm_modeler.pipeline import (
    VolumeResult,
    process_warning_satellite,
    replay_fixture,
)
from storm_modeler.settings.resolver import resolve_from_values

# Hermetic: pure registry defaults, never whatever overrides sit in a live DB.
DEFAULTS = resolve_from_values({})


def test_tornado_fixture_tracks_a_deep_cell(tornado_case):
    s = replay_fixture(tornado_case, persist=False, settings=DEFAULTS)
    assert s.warnings == 1
    assert s.volumes == 9
    assert s.cells == s.volumes  # one tracked cell per volume

    # Same track id across all volumes; realistic depth inside the window.
    track_ids = {c.track_id for r in s.results for c in r.cells}
    assert track_ids == {1}
    max_depth = max(c.depth_km for r in s.results for c in r.cells)
    assert max_depth > 6.0

    # Every cell falls inside the warning polygon.
    w = s.results[0].warning
    for r in s.results:
        for c in r.cells:
            assert w.polygon.buffer(0.05).contains(
                __import__("shapely.geometry", fromlist=["Point"]).Point(
                    c.seed_lon, c.seed_lat
                )
            )


def test_ap_fixture_admits_zero_cells(ap_case):
    s = replay_fixture(ap_case, persist=False, settings=DEFAULTS)
    assert s.warnings == 1
    assert s.volumes == 1
    assert s.cells == 0


def _sat_warning():
    s = get_site("KFWS")
    poly = Polygon([
        (s.lon - 0.3, s.lat - 0.3), (s.lon + 0.3, s.lat - 0.3),
        (s.lon + 0.3, s.lat + 0.3), (s.lon - 0.3, s.lat + 0.3),
    ])
    return Warning(
        id="SAT-TEST", event="Tornado Warning", phenomena="TO", significance="W",
        wfo="FWD", etn=1, ugc=["TXC201"], states=["TX"], polygon=poly,
        issued="2024-05-25T17:40:00Z", expires="2024-05-25T18:10:00Z",
    )


def test_satellite_pipeline_over_fixture_scenes(tmp_path):
    """process_warning_satellite tracks a cold top across scenes and tilts it
    against the nearest radar volume — fully offline via FixtureSceneSource."""
    s = get_site("KFWS")
    bbox = (s.lon - 0.6, s.lat - 0.6, s.lon + 0.6, s.lat + 0.6)
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir()
    times = ["2024-05-25T17:42:00Z", "2024-05-25T17:47:00Z", "2024-05-25T17:52:00Z"]
    for i, t in enumerate(times):
        sc = make_cold_scene(
            "GOES-19", t, bbox, s.lon + 0.02 * i, s.lat + 0.015 * i,
        )
        sc.save_npz(scenes_dir / f"scene_{i:03d}.npz")

    # A radar volume offset ~0.05 deg east of the first cold core for tilt.
    radar_cells = scit_run(make_storm_volume(
        "KFWS", s.lat, s.lon, times[0],
        core_lon=s.lon + 0.05, core_lat=s.lat, echo_top_km=10.0,
    ))
    radar_results = [VolumeResult(
        warning=_sat_warning(), site=s,
        volume=make_storm_volume(
            "KFWS", s.lat, s.lon, times[0],
            core_lon=s.lon + 0.05, core_lat=s.lat, echo_top_km=10.0,
        ),
        cells=radar_cells,
    )]

    results = process_warning_satellite(
        _sat_warning(), FixtureSceneSource(tmp_path),
        params=DEFAULTS.cloudtop, radar_results=radar_results,
    )
    assert len(results) == 3
    assert all(len(r.cloudtops) == 1 for r in results)
    # One stable track across all three scenes.
    track_ids = {c.track_id for r in results for c in r.cloudtops}
    assert track_ids == {1}
    # The first scene's cold top is tilted against the radar core (residual
    # offset after parallax correction).
    first = results[0].cloudtops[0]
    assert first.radar_track_id == radar_cells[0].track_id
    assert 0.0 < first.tilt_km < 40.0


def test_replay_is_deterministic(tornado_case):
    a = replay_fixture(tornado_case, persist=False, settings=DEFAULTS)
    b = replay_fixture(tornado_case, persist=False, settings=DEFAULTS)
    da = [c.to_dict() for r in a.results for c in r.cells]
    db = [c.to_dict() for r in b.results for c in r.cells]
    assert da == db
