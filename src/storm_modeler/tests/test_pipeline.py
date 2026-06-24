"""End-to-end (offline, no DB) pipeline tests over the shipped fixtures."""

from __future__ import annotations

from storm_modeler.config import FIXTURE_DIR
from storm_modeler.pipeline import replay_fixture
from storm_modeler.settings.resolver import resolve_from_values

# Hermetic: pure registry defaults, never whatever overrides sit in a live DB.
DEFAULTS = resolve_from_values({})


def test_tornado_fixture_tracks_a_deep_cell():
    s = replay_fixture(
        FIXTURE_DIR / "tornado_warning_case", persist=False, settings=DEFAULTS
    )
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


def test_ap_fixture_admits_zero_cells():
    s = replay_fixture(FIXTURE_DIR / "ap_case", persist=False, settings=DEFAULTS)
    assert s.warnings == 1
    assert s.volumes == 1
    assert s.cells == 0


def test_replay_is_deterministic():
    a = replay_fixture(FIXTURE_DIR / "tornado_warning_case", persist=False, settings=DEFAULTS)
    b = replay_fixture(FIXTURE_DIR / "tornado_warning_case", persist=False, settings=DEFAULTS)
    da = [c.to_dict() for r in a.results for c in r.cells]
    db = [c.to_dict() for r in b.results for c in r.cells]
    assert da == db
