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
        params = {
            "sts": _iso_z(self.start),
            "ets": _iso_z(self.end),
            "accept": "shapefile",
            # Limit to warnings; we still filter TO/SV W in code.
            "limitps": "yes",
            "ph[]": ["TO", "SV"],
            "sig[]": ["W"],
        }
        if self.states:
            params["states[]"] = self.states
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
        from shapely.geometry import mapping

        def g(*names, default=None):
            for n in names:
                if n in row and row[n] is not None:
                    return row[n]
            return default

        ugc = g("UGC", "ugc", default="")
        ugc_list = [u for u in str(ugc).split(",") if u] if ugc else []
        return {
            "phenomena": g("PHENOM", "phenomena", default=""),
            "significance": g("SIG", "significance", default=""),
            "wfo": g("WFO", "wfo", default=""),
            "etn": int(g("ETN", "eventid", default=0) or 0),
            "issued": str(g("ISSUED", "issue", default="")),
            "expires": str(g("EXPIRED", "expire", default="")),
            "ugc": ugc_list,
            "status": g("STATUS", "status", default=""),
            "geometry": mapping(row.geometry),
        }

    def warnings(self) -> Iterator[Warning]:
        for rec in self._fetch_raw():
            ph = str(rec.get("phenomena", "")).upper()
            sig = str(rec.get("significance", "")).upper()
            if not _admit(ph, sig):
                continue
            wfo = rec.get("wfo", "")
            etn = rec.get("etn", 0)
            states = sorted({u[:2] for u in rec.get("ugc", []) if len(u) >= 2})
            wid = f"{rec.get('issued','')[:10]}-{wfo}-{ph}{sig}-{etn}"
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
                issued=rec["issued"],
                expires=rec["expires"],
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
