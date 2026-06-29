"""Track reconstruction from per-volume detections (storm_modeler.tracks)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from shapely.geometry import Polygon

from storm_modeler.pipeline import VolumeResult, replay_fixture
from storm_modeler.settings.resolver import resolve_from_values
from storm_modeler.tracks import build_tracks

DEFAULTS = resolve_from_values({})


def test_tornado_fixture_folds_into_one_track(tornado_case):
    """The tornado case has one cell tracked across all nine volumes — it should
    collapse into a single :class:`StormTrack` with a nine-point history."""
    s = replay_fixture(tornado_case, persist=False, settings=DEFAULTS)
    tracks = build_tracks(s.results)

    assert len(tracks) == 1
    track = tracks[0]
    assert track.track_id == 1
    assert track.n_volumes == s.volumes == 9
    # Samples are time-ordered.
    times = [smp.valid_time for smp in track.samples]
    assert times == sorted(times)
    # Aggregates line up with the underlying cells.
    assert track.peak_dbz == max(c.max_dbz for r in s.results for c in r.cells)
    assert track.max_echo_top_km > 0
    # No satellite association in the radar-only fixture.
    assert track.min_cloud_top_c is None
    assert track.ever_overshooting is False


def _cell(track_id: int, cell_id: int, t, dbz: float, top: float,
          ct=None, ot=False):
    from storm_modeler.detection.detection_v2 import StormCell

    return StormCell(
        cell_id=cell_id, site="KFWS", valid_time=t,
        seed_lon=-97.0 + 0.01 * cell_id, seed_lat=32.0 + 0.01 * cell_id,
        seed_x=0.0, seed_y=0.0, max_dbz=dbz, area_km2=10.0,
        echo_top_km=top, base_km=1.0, depth_km=top - 1.0, n_levels=5,
        envelope=Polygon([(-97, 32), (-96.99, 32), (-96.99, 32.01)]),
        track_id=track_id, cloud_top_c=ct, overshooting_top=ot,
    )


def _result(t, cells):
    return VolumeResult(warning=None, site=None, volume=type(
        "V", (), {"valid_time": t})(), cells=cells)


def test_groups_by_track_and_unlinked_become_singletons():
    t0 = datetime(2024, 5, 25, 17, 40, tzinfo=timezone.utc)
    t1 = t0 + timedelta(minutes=5)
    results = [
        _result(t0, [
            _cell(7, 1, t0, 55.0, 10.0),
            _cell(-1, 2, t0, 40.0, 6.0),   # unlinked
        ]),
        _result(t1, [
            _cell(7, 1, t1, 60.0, 12.0, ct=-65.0, ot=True),  # OT here
            _cell(-1, 3, t1, 35.0, 5.0),   # another unlinked
        ]),
    ]
    tracks = build_tracks(results)

    # One real track (id 7) + two singletons.
    by_id = {t.key: t for t in tracks}
    assert sum(1 for t in tracks if t.track_id == 7) == 1
    assert sum(1 for t in tracks if t.track_id == -1) == 2

    # Strongest-first ordering puts track 7 at the top.
    assert tracks[0].track_id == 7
    track7 = tracks[0]
    assert track7.n_volumes == 2
    assert track7.peak_dbz == 60.0
    assert track7.max_echo_top_km == 12.0
    assert track7.min_cloud_top_c == -65.0
    assert track7.ever_overshooting is True

    # Singletons each carry exactly one sample and a distinct key.
    singles = [t for t in tracks if t.track_id == -1]
    assert all(t.n_volumes == 1 for t in singles)
    assert len({t.key for t in singles}) == 2


def test_empty_results_build_no_tracks():
    assert build_tracks([]) == []
