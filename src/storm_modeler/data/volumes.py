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
    import numpy as np
    import pyart  # type: ignore

    radar = pyart.io.read_nexrad_archive(file_obj)

    hw = grid.half_width_km * 1000.0
    nz, ny, nx = grid.nz(v_km), grid.nx(h_km), grid.nx(h_km)
    z_lim = (grid.z_min_km * 1000.0, grid.z_max_km * 1000.0)

    gridded = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(nz, ny, nx),
        grid_limits=(z_lim, (-hw, hw), (-hw, hw)),
        fields=["reflectivity"],
        weighting_function="Barnes2",
    )
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


class FixtureVolumeSource(VolumeSource):
    """Read pre-gridded ``*.npz`` volumes from a fixture's ``volumes/`` dir."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.volumes_dir = Path(fixture_dir) / "volumes"

    def volumes(self) -> Iterator[GriddedVolume]:
        files = sorted(self.volumes_dir.glob("*.npz"))
        vols = [GriddedVolume.load_npz(f) for f in files]
        vols.sort(key=lambda v: v.valid_time)  # oldest -> newest
        yield from vols
