"""Volume sources.

``VolumeSource`` yields gridded radar volumes oldest→newest for a site over a
bounded time window.

* :class:`NexradArchiveSource` lists and reads archived Level II files from the
  public ``noaa-nexrad-level2`` S3 bucket, then grids each onto the local
  analysis grid with Py-ART. The window is ``issued − 60 min … expires + 30
  min`` — fully bounded, no tailing. boto3/s3fs/pyart are imported lazily (the
  ``live`` extra); the deterministic fixture path needs none of them.
* :class:`FixtureVolumeSource` reads pre-gridded ``*.npz`` volumes from a
  replay fixture directory — the offline path Section 7 exercises.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import structlog

from ..config import DEFAULT_GRID, GridConfig
from ..models import GriddedVolume

log = structlog.get_logger(__name__)


class VolumeSource(ABC):
    """Abstract base: given a site + window, yields gridded volumes oldest→newest."""

    @abstractmethod
    def volumes(self) -> Iterator[GriddedVolume]:  # pragma: no cover - interface
        ...

    def __iter__(self) -> Iterator[GriddedVolume]:
        return self.volumes()

    def estimated_count(self) -> int:
        """Cheap count of volumes in the window for progress (0 if unknown)."""
        return 0


def bounded_window(
    issued: datetime, expires: datetime, pre_minutes: int = 60, post_minutes: int = 30
) -> tuple[datetime, datetime]:
    """The deterministic pull window for a warning (Section 2)."""
    return (
        issued - timedelta(minutes=pre_minutes),
        expires + timedelta(minutes=post_minutes),
    )


NEXRAD_BUCKET = "noaa-nexrad-level2"


class NexradArchiveSource(VolumeSource):
    """Archived Level II volumes for ``site`` within ``[start, end]``, gridded."""

    def __init__(
        self,
        site: str,
        start: datetime,
        end: datetime,
        lat0: float,
        lon0: float,
        grid: GridConfig = DEFAULT_GRID,
        h_km: float = 1.0,
        v_km: float = 0.5,
    ) -> None:
        self.site = site
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.lat0 = lat0
        self.lon0 = lon0
        self.grid = grid
        self.h_km = h_km
        self.v_km = v_km

    def _list_keys(self) -> list[str]:
        """List archive object keys in the window (lazy s3fs)."""
        import s3fs  # type: ignore

        fs = s3fs.S3FileSystem(anon=True)
        keys: list[str] = []
        day = self.start.date()
        while day <= self.end.date():
            prefix = f"{NEXRAD_BUCKET}/{day:%Y/%m/%d}/{self.site}/"
            try:
                for k in fs.ls(prefix):
                    if k.endswith("_MDM"):
                        continue
                    keys.append(k)
            except FileNotFoundError:
                pass
            day += timedelta(days=1)
        return sorted(keys)

    @staticmethod
    def _key_time(key: str) -> datetime | None:
        # .../KFWS20240525_174213_V06
        name = key.rsplit("/", 1)[-1]
        for tok in name.replace("-", "_").split("_"):
            if len(tok) >= 13 and tok[:4].isalpha():
                stamp = tok[4:]
                try:
                    return datetime.strptime(stamp[:13], "%Y%m%d_%H%M").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
        return None

    def volumes(self) -> Iterator[GriddedVolume]:
        import s3fs  # type: ignore

        fs = s3fs.S3FileSystem(anon=True)
        for key in self._list_keys():
            t = self._key_time(key)
            if t is None or not (self.start <= t <= self.end):
                continue
            log.info("nexrad.read", key=key, valid_time=t.isoformat())
            with fs.open(key, "rb") as fh:
                vol = grid_level2(
                    fh, self.site, self.lat0, self.lon0, self.grid,
                    self.h_km, self.v_km,
                )
            yield vol


def grid_level2(
    file_obj,
    site: str,
    lat0: float,
    lon0: float,
    grid: GridConfig = DEFAULT_GRID,
    h_km: float = 1.0,
    v_km: float = 0.5,
) -> GriddedVolume:
    """Grid one archived Level II volume onto the local Cartesian grid.

    Uses Py-ART (lazy import). Reflectivity is mapped onto an azimuthal
    -equidistant tangent plane centred on the radar so the result matches the
    :class:`GriddedVolume` contract SCIT consumes.
    """
    import time

    import numpy as np
    import pyart  # type: ignore

    t0 = time.perf_counter()
    radar = pyart.io.read_nexrad_archive(file_obj)
    log.info("grid.read_done", site=site, nrays=int(radar.nrays),
             nsweeps=int(radar.nsweeps), secs=round(time.perf_counter() - t0, 2))

    hw = grid.half_width_km * 1000.0
    nz, ny, nx = grid.nz(v_km), grid.nx(h_km), grid.nx(h_km)
    z_lim = (grid.z_min_km * 1000.0, grid.z_max_km * 1000.0)

    log.info("grid.interp_begin", site=site, shape=(nz, ny, nx),
             h_km=h_km, v_km=v_km)
    t1 = time.perf_counter()
    gridded = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(nz, ny, nx),
        grid_limits=(z_lim, (-hw, hw), (-hw, hw)),
        fields=["reflectivity"],
        weighting_function="Barnes2",
    )
    log.info("grid.interp_done", site=site, secs=round(time.perf_counter() - t1, 2))
    refl = np.ma.filled(
        gridded.fields["reflectivity"]["data"], np.nan
    ).astype(np.float32)

    x = gridded.x["data"].astype(np.float64)
    y = gridded.y["data"].astype(np.float64)
    z = gridded.z["data"].astype(np.float64)

    valid_time = datetime.strptime(
        gridded.time["units"].split("since")[-1].strip()[:19], "%Y-%m-%dT%H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    return GriddedVolume(
        site=site,
        valid_time=valid_time,
        reflectivity=refl,
        x=x,
        y=y,
        z=z,
        lat0=lat0,
        lon0=lon0,
    )


# --- Unidata THREDDS (anonymous HTTP) --------------------------------------

#: Unidata's THREDDS Data Server mirrors the NEXRAD Level II archive over plain
#: anonymous HTTP — no AWS credentials, no S3 (which some networks block). It
#: retains a rolling window of recent days, which covers the live-warning case.
THREDDS_CATALOG = "https://thredds.ucar.edu/thredds/catalog/nexrad/level2"
THREDDS_FILESERVER = "https://thredds.ucar.edu/thredds/fileServer"
_THREDDS_NS = {"t": "http://www.unidata.ucar.edu/namespaces/thredds/InvCatalog/v1.0"}
#: ``Level2_KFTG_20260624_2301.ar2v`` -> (date, hhmm).
_THREDDS_NAME = re.compile(r"_(\d{8})_(\d{4})")


class ThreddsLevel2Source(VolumeSource):
    """Archived Level II volumes for ``site`` in ``[start, end]`` via Unidata.

    Lists each day's THREDDS catalog, keeps the volumes whose filename time
    falls in the window, downloads each ``.ar2v`` over anonymous HTTP, and grids
    it with Py-ART — the same :class:`GriddedVolume` contract the S3 path
    produces. ``httpx`` + ``pyart`` are imported lazily (the ``live`` extra).
    """

    def __init__(
        self,
        site: str,
        start: datetime,
        end: datetime,
        lat0: float,
        lon0: float,
        grid: GridConfig = DEFAULT_GRID,
        h_km: float = 1.0,
        v_km: float = 0.5,
    ) -> None:
        self.site = site.upper()
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.lat0 = lat0
        self.lon0 = lon0
        self.grid = grid
        self.h_km = h_km
        self.v_km = v_km
        self._keys: list[tuple[datetime, str]] | None = None

    @staticmethod
    def _name_time(name: str) -> datetime | None:
        m = _THREDDS_NAME.search(name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    def _list_keys(self) -> list[tuple[datetime, str]]:
        """(`valid_time`, `urlPath`) for every volume in the window, time-sorted."""
        import httpx  # type: ignore
        from xml.etree import ElementTree as ET

        out: list[tuple[datetime, str]] = []
        day = self.start.date()
        while day <= self.end.date():
            url = f"{THREDDS_CATALOG}/{self.site}/{day:%Y%m%d}/catalog.xml"
            try:
                resp = httpx.get(url, timeout=60, follow_redirects=True)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    for ds in root.findall(".//t:dataset", _THREDDS_NS):
                        path = ds.get("urlPath")
                        name = ds.get("name") or ""
                        if not path or not name.endswith(".ar2v"):
                            continue
                        t = self._name_time(name)
                        if t is not None and self.start <= t <= self.end:
                            out.append((t, path))
            except Exception as e:  # noqa: BLE001 - skip a missing/parse-failed day
                log.info("thredds.day_skipped", day=str(day), reason=str(e).splitlines()[0])
            day += timedelta(days=1)
        out.sort(key=lambda p: p[0])
        return out

    def _keys_cached(self) -> list[tuple[datetime, str]]:
        if self._keys is None:
            self._keys = self._list_keys()
        return self._keys

    def estimated_count(self) -> int:
        return len(self._keys_cached())

    def volumes(self) -> Iterator[GriddedVolume]:
        import io
        import time

        import httpx  # type: ignore

        keys = self._keys_cached()
        log.info("thredds.window", site=self.site, n=len(keys),
                 start=self.start.isoformat(), end=self.end.isoformat())
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            for i, (t, path) in enumerate(keys, 1):
                url = f"{THREDDS_FILESERVER}/{path}"
                log.info("thredds.download_begin", index=i, total=len(keys),
                         valid_time=t.isoformat(), file=path.rsplit("/", 1)[-1])
                t0 = time.perf_counter()
                resp = client.get(url)
                resp.raise_for_status()
                mb = len(resp.content) / 1e6
                log.info("thredds.download_done", index=i, total=len(keys),
                         mb=round(mb, 2), secs=round(time.perf_counter() - t0, 2))
                log.info("thredds.grid_begin", index=i, total=len(keys))
                t1 = time.perf_counter()
                vol = grid_level2(
                    io.BytesIO(resp.content), self.site, self.lat0, self.lon0,
                    self.grid, self.h_km, self.v_km,
                )
                log.info("thredds.grid_done", index=i, total=len(keys),
                         valid_time=vol.valid_time.isoformat(),
                         secs=round(time.perf_counter() - t1, 2))
                yield vol


class FixtureVolumeSource(VolumeSource):
    """Read pre-gridded ``*.npz`` volumes from a fixture's ``volumes/`` dir."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.volumes_dir = Path(fixture_dir) / "volumes"

    def estimated_count(self) -> int:
        return len(list(self.volumes_dir.glob("*.npz")))

    def volumes(self) -> Iterator[GriddedVolume]:
        files = sorted(self.volumes_dir.glob("*.npz"))
        vols = [GriddedVolume.load_npz(f) for f in files]
        vols.sort(key=lambda v: v.valid_time)  # oldest -> newest
        yield from vols
