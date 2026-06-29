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
        phenomena: Iterable[str] | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self.start = start
        self.end = end
        self.states = sorted(s.upper() for s in states) if states else None
        # Which VTEC phenomena to admit (significance W). Defaults to both the
        # admitted types; the search form narrows it via the event checkboxes.
        chosen = [p.upper() for p in phenomena] if phenomena is not None else None
        self.phenomena = tuple(
            p for p in sorted(set(chosen)) if p in ADMITTED_PHENOMENA
        ) if chosen is not None else tuple(sorted(ADMITTED_PHENOMENA))
        self.cache_dir = Path(cache_dir or CACHE_DIR) / "iem_warnings"

    def _cache_key(self) -> str:
        st = "+".join(self.states) if self.states else "ALL"
        ph = "+".join(self.phenomena) if self.phenomena else "NONE"
        return f"{_iso_z(self.start)}_{_iso_z(self.end)}_{st}_{ph}".replace(":", "")

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
            "phenomena": list(self.phenomena),
            "significance": ["W"] * len(self.phenomena),
        }
        if self.states:
            params["states"] = self.states
        return params

    def _fetch_raw(self) -> list[dict]:
        """Fetch the SBW shapefile from IEM and return raw feature dicts.

        Reads the shapefile with pyogrio's *raw* reader + shapely — deliberately
        NOT geopandas. ``geopandas.read_file`` builds a pyproj CRS object, and
        PROJ's context init is not safe on the ``QThreadPool`` search worker (it
        segfaults — see workers.py). The raw reader returns WKB geometry + plain
        attribute arrays with no pyproj touch; the IEM service already serves
        EPSG:4326, so no reprojection is needed. ``httpx``/``pyogrio``/``shapely``
        are the ``live`` extra. Cached to disk so a second run is offline.
        """
        cache = self._cache_path()
        if cache.exists():
            log.info("iem.cache_hit", path=str(cache))
            return json.loads(cache.read_text())

        import io
        import zipfile

        import httpx  # type: ignore

        log.info("iem.fetch", sts=_iso_z(self.start), ets=_iso_z(self.end))
        resp = httpx.get(IEM_WATCHWARN_URL, params=self._request_params(), timeout=120)
        resp.raise_for_status()

        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            tmp = self.cache_dir / "_tmp"
            tmp.mkdir(parents=True, exist_ok=True)
            zf.extractall(tmp)
            # Pick the .shp from *this* zip's contents — _tmp accumulates
            # shapefiles across fetches, so globbing the dir can return a stale
            # (even empty) one.
            shp_name = next(n for n in zf.namelist() if n.endswith(".shp"))
            records = [
                self._row_to_record(attrs, geom)
                for attrs, geom in self._read_features(tmp / shp_name)
            ]

        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(records))
        return records

    @staticmethod
    def _read_features(shp_path) -> list[tuple[dict, object]]:
        """(`attrs`, `shapely geometry`) per feature, via pyogrio's raw reader.

        Avoids geopandas/pyproj entirely (no CRS object is constructed), which is
        what keeps the search worker thread from segfaulting in PROJ.
        """
        import shapely  # type: ignore
        from pyogrio.raw import read as raw_read  # type: ignore

        meta, _fids, geometry, field_data = raw_read(shp_path)
        fields = list(meta["fields"])
        out: list[tuple[dict, object]] = []
        for i in range(len(geometry)):
            attrs = {f: field_data[j][i] for j, f in enumerate(fields)}
            wkb = geometry[i]
            geom = shapely.from_wkb(wkb) if wkb is not None else None
            out.append((attrs, geom))
        return out

    @staticmethod
    def _row_to_record(attrs: dict, geom) -> dict:
        def g(*names, default=None):
            for n in names:
                if n in attrs:
                    v = attrs[n]
                    # Skip missing values, incl. float NaN for empty DBF cells
                    # (e.g. NWS_UGC on a polygon-only warning).
                    if v is not None and not (isinstance(v, float) and v != v):
                        return v
            return default

        ugc = g("UGC", "NWS_UGC", "ugc", default="")
        ugc_list = [u for u in str(ugc).split(",") if u] if ugc else []
        return {
            "phenomena": str(g("PHENOM", "phenomena", default="")),
            "significance": str(g("SIG", "significance", default="")),
            "wfo": str(g("WFO", "wfo", default="")),
            "etn": int(g("ETN", "eventid", default=0) or 0),
            # Prefer the stable *initial* VTEC issue/expire so the event's times
            # are the same on every segment row (dedup becomes order-independent).
            "issued": _normalize_ts(g("INIT_ISS", "ISSUED", "issue", default="")),
            "expires": _normalize_ts(g("INIT_EXP", "EXPIRED", "expire", default="")),
            "ugc": ugc_list,
            "status": str(g("STATUS", "status", default="")),
            "geometry": _largest_polygon_mapping(geom),
        }

    def warnings(self) -> Iterator[Warning]:
        # A single VTEC event (one wfo+phenomena+significance+etn) arrives as
        # several shapefile rows — polygon updates / continued segments — so
        # emit each warning once, keyed by its stable id.
        seen: set[str] = set()
        chosen = set(self.phenomena)
        for rec in self._fetch_raw():
            ph = str(rec.get("phenomena", "")).upper()
            sig = str(rec.get("significance", "")).upper()
            # Admit only the selected phenomena at significance W (defends in
            # code even if the server-side filter is loose).
            if sig != ADMITTED_SIGNIFICANCE or ph not in chosen:
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
