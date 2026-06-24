"""Warning sources.

``WarningSource`` yields :class:`~storm_modeler.models.Warning` objects.

* :class:`IEMHistoricalSource` queries the Iowa Environmental Mesonet archive
  of NWS storm-based warnings (complete since 2005-11-12) for a date range,
  filtering in code to VTEC phenomena ``TO``/``SV`` significance ``W``.
  Results are cached on disk keyed by date range so reruns are offline.
* :class:`FixtureWarningSource` reads a warning straight from a replay fixture
  directory — the deterministic, network-free path used by Section 7.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

import structlog

from ..config import (
    ADMITTED_PHENOMENA,
    ADMITTED_SIGNIFICANCE,
    CACHE_DIR,
    event_name,
)
from ..models import Warning

log = structlog.get_logger(__name__)


class WarningSource(ABC):
    """Abstract base: yields historical warning objects."""

    @abstractmethod
    def warnings(self) -> Iterator[Warning]:  # pragma: no cover - interface
        ...

    def __iter__(self) -> Iterator[Warning]:
        return self.warnings()


def _admit(phenomena: str, significance: str) -> bool:
    return phenomena in ADMITTED_PHENOMENA and significance == ADMITTED_SIGNIFICANCE


# The IEM watchwarn GIS endpoint (mirrors the documented lsr.py sts/ets
# pattern). Returns a zipped shapefile that GeoPandas reads directly.
IEM_WATCHWARN_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/gis/watchwarn.py"


def _iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _normalize_ts(value) -> str:
    """Normalise an IEM watchwarn timestamp to an ISO-8601 UTC string.

    The watchwarn shapefile encodes ISSUED/EXPIRED as compact UTC
    ``YYYYMMDDHHMM`` (or ``…SS``) strings, which ``datetime.fromisoformat``
    rejects. Accept those, pass ISO strings through unchanged, and leave
    anything unrecognised for the model layer to reject loudly.
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    try:  # already ISO (with or without a trailing Z)?
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        return s
    except ValueError:
        pass
    # Compact IEM UTC. Dispatch by length: strptime's field regexes backtrack,
    # so "%Y%m%d%H%M%S" would mis-parse a 12-digit string (HHMM as H,M,S).
    fmt = {12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}.get(len(s)) if s.isdigit() else None
    if fmt:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return s


def _largest_polygon_mapping(geom):
    """GeoJSON mapping of ``geom``, reducing a MultiPolygon to its largest part.

    Downstream rendering (map envelopes, the 3D prism) assumes a single
    ``Polygon`` exterior; a few SBWs arrive as MultiPolygons, so keep the
    dominant lobe rather than crashing on ``.exterior``.
    """
    from shapely.geometry import mapping

    if geom is not None and getattr(geom, "geom_type", "") == "MultiPolygon":
        geom = max(geom.geoms, key=lambda g: g.area)
    return mapping(geom)


class IEMHistoricalSource(WarningSource):
    """Pull SBW polygons by time range from the IEM watchwarn service.

    Parameters
    ----------
    start, end:
        UTC bounds of the pull window.
    states:
        Optional list of two-letter state codes to constrain the query.
    cache_dir:
        On-disk cache root; a pull for a given (start, end, states) key is
        written once and replayed offline thereafter.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        states: Iterable[str] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.states = sorted(s.upper() for s in states) if states else None
        self.cache_dir = Path(cache_dir or CACHE_DIR) / "iem_warnings"

    def _cache_key(self) -> str:
        st = "+".join(self.states) if self.states else "ALL"
        return f"{_iso_z(self.start)}_{_iso_z(self.end)}_{st}".replace(":", "")

    def _cache_path(self) -> Path:
        return self.cache_dir / f"{self._cache_key()}.json"

    def _request_params(self) -> dict:
        # IEM watchwarn GIS params (verified against the live service):
        #  * ``timeopt=1`` selects start/end *range* mode — without it ``sts``/
        #    ``ets`` are ignored and the service returns current warnings.
        #  * ``phenomena``/``significance`` are positionally-aligned lists, so
        #    TO and SV each need their own ``W``. ``limitps=yes`` activates them.
        #  * ``states`` is the documented state filter (best-effort: the service
        #    does not constrain SBW polygons by it, so we still filter in code).
        params = {
            "sts": _iso_z(self.start),
            "ets": _iso_z(self.end),
            "timeopt": "1",
            "accept": "shapefile",
            "limitps": "yes",
            "phenomena": ["TO", "SV"],
            "significance": ["W", "W"],
        }
        if self.states:
            params["states"] = self.states
        return params

    def _fetch_raw(self) -> list[dict]:
        """Fetch the SBW shapefile from IEM and return raw feature dicts.

        Lazily imports httpx + geopandas (the ``live`` extra). Cached to disk so
        a second run is offline.
        """
        cache = self._cache_path()
        if cache.exists():
            log.info("iem.cache_hit", path=str(cache))
            return json.loads(cache.read_text())

        import io
        import zipfile

        import geopandas as gpd  # type: ignore
        import httpx  # type: ignore

        log.info("iem.fetch", sts=_iso_z(self.start), ets=_iso_z(self.end))
        resp = httpx.get(IEM_WATCHWARN_URL, params=self._request_params(), timeout=120)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            tmp = self.cache_dir / "_tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            zf.extractall(tmp)
            shp = next(tmp.glob("*.shp"))
            gdf = gpd.read_file(shp).to_crs("EPSG:4326")

        records = []
        for _, row in gdf.iterrows():
            records.append(self._row_to_record(row))

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(records))
        return records

    @staticmethod
    def _row_to_record(row) -> dict:
        def g(*names, default=None):
            for n in names:
                if n in row:
                    v = row[n]
                    # Skip missing values, incl. GeoPandas' float NaN for empty
                    # DBF cells (e.g. NWS_UGC on a polygon-only warning).
                    if v is not None and not (isinstance(v, float) and v != v):
                        return v
            return default

        ugc = g("UGC", "NWS_UGC", "ugc", default="")
        ugc_list = [u for u in str(ugc).split(",") if u] if ugc else []
        return {
            "phenomena": g("PHENOM", "phenomena", default=""),
            "significance": g("SIG", "significance", default=""),
            "wfo": g("WFO", "wfo", default=""),
            "etn": int(g("ETN", "eventid", default=0) or 0),
            # Prefer the stable *initial* VTEC issue/expire so the event's times
            # are the same on every segment row (dedup becomes order-independent).
            "issued": _normalize_ts(g("INIT_ISS", "ISSUED", "issue", default="")),
            "expires": _normalize_ts(g("INIT_EXP", "EXPIRED", "expire", default="")),
            "ugc": ugc_list,
            "status": g("STATUS", "status", default=""),
            "geometry": _largest_polygon_mapping(row.geometry),
        }

    def warnings(self) -> Iterator[Warning]:
        # A single VTEC event (one wfo+phenomena+significance+etn) arrives as
        # several shapefile rows — polygon updates / continued segments — so
        # emit each warning once, keyed by its stable id.
        seen: set[str] = set()
        for rec in self._fetch_raw():
            ph = str(rec.get("phenomena", "")).upper()
            sig = str(rec.get("significance", "")).upper()
            if not _admit(ph, sig):
                continue
            wfo = rec.get("wfo", "")
            etn = rec.get("etn", 0)
            states = sorted({u[:2] for u in rec.get("ugc", []) if len(u) >= 2})
            # Normalise once so a cache written by an older build (compact IEM
            # timestamps) self-heals, and the id's date prefix is consistent.
            issued = _normalize_ts(rec["issued"])
            expires = _normalize_ts(rec["expires"])
            wid = f"{issued[:10]}-{wfo}-{ph}{sig}-{etn}"
            if wid in seen:
                continue
            seen.add(wid)
            yield Warning(
                id=wid,
                event=event_name(ph, sig),
                phenomena=ph,
                significance=sig,
                wfo=wfo,
                etn=etn,
                ugc=rec.get("ugc", []),
                states=states,
                polygon=rec["geometry"],
                issued=issued,
                expires=expires,
            )


class ListWarningSource(WarningSource):
    """Wrap an in-memory list of warnings, applying the TO/SV-W filter."""

    def __init__(self, warnings: Iterable[Warning]) -> None:
        self._warnings = [
            w for w in warnings if _admit(w.phenomena, w.significance)
        ]

    def warnings(self) -> Iterator[Warning]:
        yield from self._warnings


class FixtureWarningSource(WarningSource):
    """Read a single warning (``warning.json``) from a replay fixture dir."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.fixture_dir = Path(fixture_dir)

    def warnings(self) -> Iterator[Warning]:
        wfile = self.fixture_dir / "warning.json"
        w = Warning.from_json_file(wfile)
        if _admit(w.phenomena, w.significance):
            yield w
        else:  # pragma: no cover - fixtures are always admissible
            log.warning("fixture.filtered_out", id=w.id)
