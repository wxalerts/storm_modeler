"""The IEM source admits only VTEC TO/SV significance-W events (Section 1a).

Proves the in-code filter offline by feeding a mixed raw feature set through
``IEMHistoricalSource.warnings()`` (the network fetch is stubbed), so no live
IEM access is needed to verify the admission rule.
"""

from __future__ import annotations

from datetime import datetime, timezone

from shapely.geometry import mapping
from shapely.geometry import box

from storm_modeler.data.warnings import (
    FixtureWarningSource,
    IEMHistoricalSource,
    ListWarningSource,
)
from storm_modeler.models import Warning


def _raw(ph, sig, etn):
    return {
        "phenomena": ph,
        "significance": sig,
        "wfo": "FWD",
        "etn": etn,
        "issued": "2024-05-06T20:00:00Z",
        "expires": "2024-05-06T20:45:00Z",
        "ugc": ["TXC201"],
        "geometry": mapping(box(-97.5, 32.4, -97.0, 32.9)),
    }


def test_iem_filter_admits_only_to_sv_w(monkeypatch):
    mixed = [
        _raw("TO", "W", 1),   # admit
        _raw("SV", "W", 2),   # admit
        _raw("FF", "W", 3),   # flash flood — drop
        _raw("TO", "A", 4),   # tornado watch — drop
        _raw("SV", "Y", 5),   # advisory — drop
        _raw("MA", "W", 6),   # marine — drop
        _raw("SV", "W", 7),   # admit
    ]
    src = IEMHistoricalSource(
        datetime(2024, 5, 6, 18, tzinfo=timezone.utc),
        datetime(2024, 5, 7, 6, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(src, "_fetch_raw", lambda: mixed)

    admitted = list(src.warnings())
    assert [(w.phenomena, w.significance) for w in admitted] == [
        ("TO", "W"), ("SV", "W"), ("SV", "W")
    ]
    assert all(w.phenomena in {"TO", "SV"} and w.significance == "W" for w in admitted)


def test_list_source_applies_same_filter():
    def _w(ph, sig, etn):
        return Warning(
            id=f"x{etn}", event="e", phenomena=ph, significance=sig, wfo="FWD",
            etn=etn, ugc=["TXC201"], states=["TX"],
            polygon=box(-97.5, 32.4, -97.0, 32.9),
            issued="2024-05-06T20:00:00Z", expires="2024-05-06T20:45:00Z",
        )

    src = ListWarningSource([_w("TO", "W", 1), _w("FF", "W", 2), _w("SV", "W", 3)])
    out = list(src.warnings())
    assert [w.etn for w in out] == [1, 3]
