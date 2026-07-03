"""HRRR 0 °C freezing-level ingest (anonymous NOAA S3).

The HRRR surface file (``wrfsfcf00``, the hourly CONUS analysis) carries the
height of the 0 °C isotherm (``HGT : 0C isotherm``, metres MSL) on the 3 km
Lambert-conformal CONUS grid. The files are mirrored on AWS in the
``noaa-hrrr-bdp-pds`` bucket under ``hrrr.<YYYYMMDD>/conus/`` — one ~400 MB
GRIB2 per run, with a ``.idx`` inventory beside it. We never pull the whole
file: the ``.idx`` gives each field's byte offset, so a ranged GET fetches just
the single freezing-level message (~1 MB), which pygrib decodes.

The Lambert conformal → lon/lat resampling mirrors the ABI path
(:mod:`.satellite`): the target regular lon/lat raster over the warning bbox is
**forward-projected** into LCC grid coordinates in pure NumPy (no pyproj — safe
on a worker thread, see ``models.PROJ_LOCK``) and the HRRR field, regular in
LCC space, is sampled there.

``boto3``/``pygrib`` ride in the ``live`` extra and are imported lazily, so the
offline pipeline never needs them. No Qt and no GUI work here —
:class:`storm_modeler.workers.HRRRWorker` drives it off the GUI thread.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import structlog

from ..models import FreezingLevelGrid

log = structlog.get_logger(__name__)

# HRRR lives in us-east-1, public (unsigned) reads.
S3_REGION = "us-east-1"
BUCKET = "noaa-hrrr-bdp-pds"

#: The GRIB inventory line we want: variable + level exactly as wgrib2 prints
#: them in the ``.idx`` (``…:HGT:0C isotherm:anl:``).
IDX_VAR = "HGT"
IDX_LEVEL = "0C isotherm"


def freezing_level_key(run: datetime) -> str:
    """S3 key of the HRRR CONUS surface analysis for the ``run`` hour."""
    return f"hrrr.{run:%Y%m%d}/conus/hrrr.t{run:%H}z.wrfsfcf00.grib2"


def idx_byte_range(idx_text: str, var: str = IDX_VAR, level: str = IDX_LEVEL
                   ) -> tuple[int, int | None] | None:
    """Byte range ``(start, end)`` of one field within a GRIB2, from its ``.idx``.

    Inventory lines look like ``147:95870907:d=2024052500:HGT:0C isotherm:anl:``
    — message number, byte offset, ref time, variable, level, forecast. The
    field's bytes run from its own offset to the next line's offset − 1 (``end``
    is ``None`` for the last message: range to EOF).
    """
    lines = [ln for ln in idx_text.splitlines() if ln.strip()]
    for k, ln in enumerate(lines):
        parts = ln.split(":")
        if len(parts) >= 5 and parts[3] == var and parts[4] == level:
            start = int(parts[1])
            end = int(lines[k + 1].split(":")[1]) - 1 if k + 1 < len(lines) else None
            return start, end
    return None


def lcc_forward(
    lon: np.ndarray,
    lat: np.ndarray,
    lon0: float,
    lat0: float,
    lat1: float,
    lat2: float,
    radius: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Geodetic → Lambert-conformal-conic (x, y) metres, spherical earth.

    Pure NumPy implementation of the standard LCC forward formulas (Snyder,
    "Map Projections", eq. 15-1…15-4, spherical form) — HRRR's grid is defined
    on a sphere of radius 6 371 229 m. Matches PROJ's ``+proj=lcc +R=…`` to
    sub-metre over CONUS (unit-tested against pyproj).
    """
    phi = np.radians(lat)
    p0, p1, p2 = np.radians(lat0), np.radians(lat1), np.radians(lat2)
    # Wrap the longitude difference to (-180, 180] so the cone never unrolls
    # the wrong way.
    dlam = np.radians(((np.asarray(lon, dtype=np.float64) - lon0 + 180.0) % 360.0) - 180.0)

    if abs(lat1 - lat2) < 1e-9:
        n = np.sin(p1)
    else:
        n = (np.log(np.cos(p1) / np.cos(p2))
             / np.log(np.tan(np.pi / 4 + p2 / 2) / np.tan(np.pi / 4 + p1 / 2)))
    f = np.cos(p1) * np.tan(np.pi / 4 + p1 / 2) ** n / n
    rho = radius * f / np.tan(np.pi / 4 + phi / 2) ** n
    rho0 = radius * f / np.tan(np.pi / 4 + p0 / 2) ** n
    x = rho * np.sin(n * dlam)
    y = rho0 - rho * np.cos(n * dlam)
    return x, y


def grid_from_message(
    msg,
    valid_time: datetime,
    bbox: tuple[float, float, float, float],
    res_deg: float,
    model: str = "HRRR",
) -> FreezingLevelGrid:
    """Resample one pygrib LCC message onto a regular lon/lat raster over ``bbox``.

    Reads the field and its grid definition from the message, builds the LCC
    grid axes from the first-grid-point position + spacing, forward-projects the
    target lon/lat mesh into LCC space (pure NumPy), and samples with a regular
    grid interpolator — the same inverse-mapping strategy as the ABI
    reprojection. Never calls ``msg.latlons()`` (it builds pyproj transformers,
    unsafe on a worker thread).
    """
    from scipy.interpolate import RegularGridInterpolator

    vals = np.ma.filled(np.asarray(msg.values, dtype=np.float64), np.nan)
    ny, nx = vals.shape
    proj = msg.projparams  # {'proj': 'lcc', 'lon_0': …, 'lat_1': …, 'a': …}
    lon0 = float(proj["lon_0"])
    lon0 = lon0 - 360.0 if lon0 > 180.0 else lon0
    lat0 = float(proj.get("lat_0", proj["lat_1"]))
    lat1 = float(proj["lat_1"])
    lat2 = float(proj.get("lat_2", lat1))
    radius = float(proj.get("a", 6371229.0))

    lon_first = float(msg["longitudeOfFirstGridPointInDegrees"])
    lon_first = lon_first - 360.0 if lon_first > 180.0 else lon_first
    lat_first = float(msg["latitudeOfFirstGridPointInDegrees"])
    dx = float(msg["DxInMetres"])
    dy = float(msg["DyInMetres"])

    x_first, y_first = lcc_forward(
        np.float64(lon_first), np.float64(lat_first), lon0, lat0, lat1, lat2, radius
    )
    xs = float(x_first) + np.arange(nx, dtype=np.float64) * dx
    if int(msg["jScansPositively"]):
        ys = float(y_first) + np.arange(ny, dtype=np.float64) * dy
    else:  # row 0 northernmost — flip to ascending y for the interpolator
        ys = float(y_first) - np.arange(ny, dtype=np.float64) * dy
        ys, vals = ys[::-1], vals[::-1, :]

    lon_min, lat_min, lon_max, lat_max = bbox
    lons = np.arange(lon_min, lon_max + res_deg / 2, res_deg, dtype=np.float64)
    lats = np.arange(lat_max, lat_min - res_deg / 2, -res_deg, dtype=np.float64)
    LON, LAT = np.meshgrid(lons, lats)
    x_q, y_q = lcc_forward(LON, LAT, lon0, lat0, lat1, lat2, radius)

    interp = RegularGridInterpolator((ys, xs), vals, bounds_error=False,
                                     fill_value=np.nan)
    heights = interp(np.column_stack([y_q.ravel(), x_q.ravel()]))
    heights = heights.reshape(LAT.shape).astype(np.float32)
    return FreezingLevelGrid(
        model=model, valid_time=valid_time,
        heights_m=heights, lons=lons, lats=lats, bbox=bbox,
    )


class HRRRSource:
    """Hourly HRRR freezing-level grids for a warning's window, on lon/lat.

    For every top-of-hour analysis spanning ``[start, end]``: fetch the run's
    ``.idx``, locate the ``HGT : 0C isotherm`` message, ranged-GET just those
    bytes with an unsigned boto3 client, decode with pygrib, and resample onto a
    regular lon/lat raster over ``bbox``. Missing runs (archive gaps) are
    skipped, not fatal. ``boto3``/``pygrib`` are the ``live`` extra. Sub-step
    lines go to an optional status callback for the GUI.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
        target_res_deg: float = 0.03,
    ) -> None:
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.bbox = bbox
        self.target_res_deg = float(target_res_deg)
        self._status: Callable[[str], None] | None = None

    def set_status_callback(self, cb: Callable[[str], None] | None) -> None:
        self._status = cb

    def _emit(self, msg: str) -> None:
        if self._status is not None:
            try:
                self._status(msg)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _client():
        import boto3  # type: ignore  # noqa: PLC0415
        from botocore import UNSIGNED  # type: ignore  # noqa: PLC0415
        from botocore.config import Config  # type: ignore  # noqa: PLC0415

        return boto3.client(
            "s3", region_name=S3_REGION, config=Config(signature_version=UNSIGNED)
        )

    def runs(self) -> list[datetime]:
        """Top-of-hour analysis times covering the window (floor(start)…end)."""
        out = []
        cur = self.start.replace(minute=0, second=0, microsecond=0)
        while cur <= self.end:
            out.append(cur)
            cur += timedelta(hours=1)
        return out or [self.start.replace(minute=0, second=0, microsecond=0)]

    def estimated_count(self) -> int:
        return len(self.runs())

    @staticmethod
    def _decode(content: bytes, valid_time: datetime,
                bbox: tuple[float, float, float, float],
                res_deg: float) -> FreezingLevelGrid:
        """Decode one GRIB2 message's bytes (pygrib wants a real file)."""
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        import pygrib  # type: ignore  # noqa: PLC0415

        tmp = tempfile.NamedTemporaryFile(suffix=".grib2", delete=False)
        try:
            tmp.write(content)
            tmp.flush()
            tmp.close()
            grbs = pygrib.open(tmp.name)
            try:
                msg = grbs.message(1)
                return grid_from_message(msg, valid_time, bbox, res_deg)
            finally:
                grbs.close()
        finally:
            os.unlink(tmp.name)

    def grids(self, cancel: threading.Event | None = None) -> Iterator[FreezingLevelGrid]:
        """Freezing-level grids in the window, oldest→newest (gaps skipped)."""
        cancel = cancel or threading.Event()
        runs = self.runs()
        n = len(runs)
        log.info("hrrr.window", bucket=BUCKET, runs=n,
                 start=self.start.isoformat(), end=self.end.isoformat())
        self._emit(f"{n} HRRR analysis hour(s) in the window…")
        s3 = self._client()
        for i, run in enumerate(runs, 1):
            if cancel.is_set():
                break
            key = freezing_level_key(run)
            try:
                idx = s3.get_object(Bucket=BUCKET, Key=key + ".idx")["Body"].read()
                span = idx_byte_range(idx.decode("utf-8", "replace"))
                if span is None:
                    log.info("hrrr.field_missing", key=key)
                    continue
                start, end = span
                rng = f"bytes={start}-" if end is None else f"bytes={start}-{end}"
                self._emit(f"Downloading 0°C level {i}/{n}   {run:%H:%MZ}")
                content = s3.get_object(Bucket=BUCKET, Key=key, Range=rng)["Body"].read()
                grid = self._decode(content, run, self.bbox, self.target_res_deg)
            except Exception as e:  # noqa: BLE001 - skip a missing/failed run
                log.info("hrrr.run_skipped", key=key, reason=str(e).splitlines()[0])
                continue
            yield grid

    def __iter__(self) -> Iterator[FreezingLevelGrid]:
        return self.grids()


class FixtureFreezingLevelSource:
    """Read pre-resampled ``*.npz`` freezing-level grids from ``<dir>/hrrr/``."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.hrrr_dir = Path(fixture_dir) / "hrrr"

    def set_status_callback(self, cb) -> None:  # parity with HRRRSource
        pass

    def estimated_count(self) -> int:
        return len(list(self.hrrr_dir.glob("*.npz")))

    def grids(self, cancel: threading.Event | None = None) -> Iterator[FreezingLevelGrid]:
        files = sorted(self.hrrr_dir.glob("*.npz"))
        out = [FreezingLevelGrid.load_npz(f) for f in files]
        out.sort(key=lambda g: g.valid_time)
        for g in out:
            if cancel is not None and cancel.is_set():
                break
            yield g

    def __iter__(self) -> Iterator[FreezingLevelGrid]:
        return self.grids()
