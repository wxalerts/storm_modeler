"""Settings registry validation and (DB-gated) store round-trip."""

from __future__ import annotations

import os

import pytest

from storm_modeler.settings.registry import REGISTRY, get_spec
from storm_modeler.settings.resolver import DetectionParams, resolve_from_values


def test_registry_validation_bounds_and_choices():
    spec = get_spec("seed_dbz")
    assert spec.validate("50") == 50.0
    with pytest.raises(ValueError):
        spec.validate(999)  # above max
    cont = get_spec("continuity_levels")
    assert cont.validate(3) == 3
    color = get_spec("dbz_color_table")
    with pytest.raises(ValueError):
        color.validate("rainbow")  # not a choice


def test_every_detection_key_is_registered():
    from storm_modeler.settings.registry import DETECTION_KEYS

    for k in DETECTION_KEYS:
        get_spec(k)  # raises if missing
    # DetectionParams fields all come from registered keys.
    for f in DetectionParams().__dataclass_fields__:
        assert f in {s.key for s in REGISTRY}


def test_resolver_projects_typed_params():
    rs = resolve_from_values({"seed_dbz": 55, "track_max_km": 20})
    p = rs.detection
    assert p.seed_dbz == 55.0 and p.track_max_km == 20.0
    assert p.settings_hash != DetectionParams().settings_hash


def test_local_store_persists_overrides_without_db(tmp_path):
    """No PG_DSN: overrides must persist to the local JSON file and survive a
    fresh resolve (the desktop 'settings won't save' regression)."""
    from storm_modeler.settings.resolver import resolve
    from storm_modeler.settings.store import LocalSettingsStore, open_store

    path = tmp_path / "settings.json"
    assert resolve(None, path).values["base_dbz"] == 30.0  # registry default

    with open_store(None, path) as store:
        assert isinstance(store, LocalSettingsStore)
        store.set_many({"base_dbz": 45, "seed_min_separation_km": 12})

    # A brand-new resolve reads it back — change is durable, not just in-memory.
    rs = resolve(None, path)
    assert rs.values["base_dbz"] == 45.0
    assert rs.detection.seed_min_separation_km == 12.0

    # Out-of-range writes are still rejected by the registry spec.
    with pytest.raises(ValueError):
        with open_store(None, path) as store:
            store.set("base_dbz", 999)


def test_open_store_returns_none_for_pure_defaults():
    """No dsn and no local path → no store (pure registry defaults path)."""
    from storm_modeler.settings.store import open_store

    assert open_store(None, None) is None


@pytest.mark.skipif(not os.environ.get("PG_DSN"), reason="needs PG_DSN")
def test_store_round_trip_changes_resolved_params():
    from storm_modeler.settings.resolver import resolve
    from storm_modeler.settings.store import SettingsStore

    with SettingsStore() as store:
        store.set("seed_dbz", 63)
        try:
            assert resolve().detection.seed_dbz == 63.0
        finally:
            store.unset("seed_dbz")
