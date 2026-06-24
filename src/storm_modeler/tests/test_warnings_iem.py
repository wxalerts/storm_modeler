"""IEM watchwarn parsing + request-shape regression tests (offline).

These lock in the fixes for the live-search path verified against the real IEM
service: the GIS request parameters, compact-timestamp normalisation, empty-UGC
(NaN) handling, MultiPolygon reduction, and per-event de-duplication. No network
or geopandas needed — rows are synthesised to mirror the shapefile schema.
"""

from __future__ import annotations

from datetime import datetime, timezone

from shapely.geometry import MultiPolygon, box, mapping

from storm_modeler.data.warnings import IEMHistoricalSource, _normalize_ts


class _Row(dict):
    """Minimal stand-in for a GeoPandas row: dict access + ``.geometry``."""

    @property
    def geometry(self):
        return self["geometry"]


# --- timestamp normalisation ------------------------------------------------

def test_normalize_ts_compact_and_iso():
    assert _normalize_ts("202405251142") == "2024-05-25T11:42:00+00:00"   # YYYYMMDDHHMM
    assert _normalize_ts("20240525114205") == "2024-05-25T11:42:05+00:00"  # +seconds
    iso = "2024-05-25T11:42:00+00:00"
    assert _normalize_ts(iso) == iso            # ISO passes through
    assert _normalize_ts("2024-05-25T11:42Z") == "2024-05-25T11:42Z"
    assert _normalize_ts("") == ""
    assert _normalize_ts(None) == ""


# --- request parameters -----------------------------------------------------

def test_request_params_match_iem_contract():
    src = IEMHistoricalSource(
        datetime(2024, 5, 25, 11, tzinfo=timezone.utc),
        datetime(2024, 5, 25, 14, tzinfo=timezone.utc),
        states=["TX", "OK"],
    )
    p = src._request_params()
    assert p["timeopt"] == "1"                      # range mode, else sts/ets ignored
    assert p["sts"] == "2024-05-25T11:00Z"
    assert p["ets"] == "2024-05-25T14:00Z"
    assert p["accept"] == "shapefile"
    # phenomena/significance are positionally aligned, each at significance W.
    assert sorted(p["phenomena"]) == ["SV", "TO"]
    assert p["significance"] == ["W"] * len(p["phenomena"])
    assert len(p["phenomena"]) == len(p["significance"])
    assert p["states"] == ["OK", "TX"]              # sorted, upper-cased


# --- row -> record ----------------------------------------------------------

def _row(**over):
    base = dict(
        WFO="FWD", ISSUED="202405251142", EXPIRED="202405251215",
        PHENOM="TO", SIG="W", ETN=84, NWS_UGC=float("nan"),
        geometry=box(-97.5, 32.4, -97.0, 32.9),
    )
    base.update(over)
    return _Row(base)


def test_row_to_record_normalises_and_cleans():
    rec = IEMHistoricalSource._row_to_record(_row())
    assert rec["issued"] == "2024-05-25T11:42:00+00:00"
    assert rec["expires"] == "2024-05-25T12:15:00+00:00"
    assert rec["phenomena"] == "TO" and rec["significance"] == "W"
    assert rec["etn"] == 84
    assert rec["ugc"] == []                         # NaN NWS_UGC -> empty, not ['nan']
    assert rec["geometry"]["type"] == "Polygon"


def test_row_to_record_reduces_multipolygon_to_largest():
    mp = MultiPolygon([box(0, 0, 1, 1), box(0, 0, 4, 4)])  # second lobe larger
    rec = IEMHistoricalSource._row_to_record(_row(geometry=mp))
    assert rec["geometry"]["type"] == "Polygon"
    # The 4x4 lobe (area 16) wins over the 1x1 (area 1).
    from shapely.geometry import shape
    assert shape(rec["geometry"]).area == 16.0


# --- warnings(): admit filter + de-duplication ------------------------------

def test_warnings_dedupes_repeated_vtec_event(monkeypatch):
    poly = mapping(box(-97.5, 32.4, -97.0, 32.9))

    def rec(etn, status):
        return {
            "phenomena": "TO", "significance": "W", "wfo": "FWD", "etn": etn,
            "issued": "202405251142", "expires": "202405251215",
            "ugc": [], "status": status, "geometry": poly,
        }

    src = IEMHistoricalSource(
        datetime(2024, 5, 25, 11, tzinfo=timezone.utc),
        datetime(2024, 5, 25, 14, tzinfo=timezone.utc),
    )
    # Same event (etn 84) three times + a distinct event (etn 85).
    monkeypatch.setattr(src, "_fetch_raw", lambda: [
        rec(84, "NEW"), rec(84, "CON"), rec(84, "EXP"), rec(85, "NEW"),
    ])
    out = list(src.warnings())
    assert [w.etn for w in out] == [84, 85]         # de-duplicated by id
    assert out[0].id == "2024-05-25-FWD-TOW-84"
    assert out[0].issued.isoformat() == "2024-05-25T11:42:00+00:00"


def test_phenomena_selection_filters_results(monkeypatch):
    poly = mapping(box(-97.5, 32.4, -97.0, 32.9))

    def rec(ph, etn):
        return {
            "phenomena": ph, "significance": "W", "wfo": "FWD", "etn": etn,
            "issued": "202405251142", "expires": "202405251215",
            "ugc": [], "status": "NEW", "geometry": poly,
        }

    raw = [rec("TO", 1), rec("SV", 2)]

    # Tornado-only: the SV warning is excluded, and the request asks for TO only.
    src = IEMHistoricalSource(
        datetime(2024, 5, 25, 11, tzinfo=timezone.utc),
        datetime(2024, 5, 25, 14, tzinfo=timezone.utc),
        phenomena=["TO"],
    )
    monkeypatch.setattr(src, "_fetch_raw", lambda: raw)
    assert [w.phenomena for w in src.warnings()] == ["TO"]
    assert src._request_params()["phenomena"] == ["TO"]
    assert "_TO" in src._cache_key()             # cache split by selection
