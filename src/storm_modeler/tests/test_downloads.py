"""Round-trip tests for the on-disk download cache (persistence across launches)."""

from __future__ import annotations

from storm_modeler.data.volumes import FixtureVolumeSource
from storm_modeler.data.warnings import FixtureWarningSource
from storm_modeler.downloads import DownloadStore


def test_save_and_reload_round_trip(tmp_path, tornado_case):
    store = DownloadStore(tmp_path)
    warning = next(iter(FixtureWarningSource(tornado_case)))
    volumes = list(FixtureVolumeSource(tornado_case))
    assert volumes  # fixture has volumes to cache

    store.save_warning(warning)
    for v in volumes:
        store.save_volume(warning.id, v)

    # The warning comes back with identity + times intact.
    reloaded = store.warnings()
    assert [w.id for w in reloaded] == [warning.id]
    assert reloaded[0].issued == warning.issued
    assert reloaded[0].event == warning.event

    # Every volume is replayable, oldest→newest, matching the saved set.
    assert store.volume_count(warning.id) == len(volumes)
    replayed = list(store.volume_source(warning.id))
    assert [v.valid_time for v in replayed] == sorted(v.valid_time for v in volumes)


def test_save_warning_is_idempotent(tmp_path, tornado_case):
    store = DownloadStore(tmp_path)
    warning = next(iter(FixtureWarningSource(tornado_case)))
    store.save_warning(warning)
    store.save_warning(warning)  # no duplicate dir / no crash
    assert [w.id for w in store.warnings()] == [warning.id]
    assert store.has(warning.id)


def test_empty_store_returns_no_warnings(tmp_path):
    assert DownloadStore(tmp_path / "missing").warnings() == []
