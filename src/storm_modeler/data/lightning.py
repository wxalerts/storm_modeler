"""GOES GLM lightning ingest (anonymous NOAA S3).

The Geostationary Lightning Mapper (GLM) L2 ``LCFA`` product carries per-flash
lat/lon + time, mirrored on AWS in the ``noaa-goes16``/``noaa-goes19`` buckets
(GOES-East) under ``GLM-L2-LCFA/<YYYY>/<DDD>/<HH>/`` — one ~20-second file each.
We list the hours spanning a warning's data window, download the files
concurrently (unsigned boto3, same fast path as the NEXRAD pull), parse each
NetCDF, and keep the flashes inside the warning's padded bounding box.

``boto3`` ships in the ``live`` extra; ``netCDF4`` rides along with ``arm-pyart``
(also ``live``). Both are imported lazily so the offline pipeline never needs
them. This module does no Qt and no GUI work — :class:`LightningWorker` drives it
off the GUI thread.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

import structlog

log = structlog.get_logger(__name__)

# GOES imagery/GLM live in us-east-1, public (unsigned) reads.
S3_REGION = "us-east-1"

#: GOES-19 replaced GOES-16 as the operational GOES-East satellite on this date;
#: before it, GLM-East data lives in ``noaa-goes16``. (GLM coverage from G16/G19
#: at -75.2° spans CONUS, so GOES-East suffices for the IEM warning set.)
GOES19_OPERATIONAL = date(2025, 4, 4)

#: GLM (and all GOES product) times are seconds since the J2000 epoch,
#: 2000-01-01 12:00:00 UTC.
GLM_EPOCH = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

#: ``OR_GLM-L2-LCFA_G16_sYYYYDDDHHMMSSt_eYYYYDDDHHMMSSt_cYYYYDDDHHMMSSt.nc`` —
#: the ``s`` (start) stamp is year(4) doy(3) hour(2) min(2) sec(2) tenths(1).
_LCFA_NAME = re.compile(r"OR_GLM-L2-LCFA_G\d{2}_s(\d{14})_e\d{14}_c\d{14}\.nc$")


@dataclass(frozen=True)
class Flash:
    """One GLM flash: location, time (UTC), and optical energy (Joules)."""

    lat: float
    lon: float
    time: datetime
    energy: float


def bucket_for_date(d: date) -> tuple[str, str]:
    """(bucket, satellite label) for the operational GOES-East GLM on *d*."""
    if d >= GOES19_OPERATIONAL:
        return "noaa-goes19", "GOES-19 (East)"
    return "noaa-goes16", "GOES-16 (East)"


def bin_nearest(
    flashes: list[Flash], valid_time: datetime, volume_times: Iterable[datetime]
) -> list[Flash]:
    """Flashes whose nearest volume (in time) is *valid_time*.

    Each flash is assigned to the closest volume; the boundary between two
    adjacent volumes is the midpoint of their valid times, so the volumes
    partition the timeline with no gaps or overlaps. With a single volume time,
    every flash maps to it.
    """
    if not flashes:
        return []
    times = sorted(set(volume_times) | {valid_time})
    i = times.index(valid_time)
    lo = times[i - 1] + (valid_time - times[i - 1]) / 2 if i > 0 else None
    hi = valid_time + (times[i + 1] - valid_time) / 2 if i < len(times) - 1 else None
    return [f for f in flashes
            if (lo is None or f.time > lo) and (hi is None or f.time <= hi)]


def _name_time(name: str) -> datetime | None:
    """Parse an LCFA filename's start stamp to a UTC datetime (or ``None``)."""
    m = _LCFA_NAME.search(name)
    if not m:
        return None
    s = m.group(1)
    try:
        year, doy, hh, mm, ss, tenths = (
            int(s[0:4]), int(s[4:7]), int(s[7:9]), int(s[9:11]),
            int(s[11:13]), int(s[13:14]),
        )
    except ValueError:
        return None
    base = datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=doy - 1)
    return base + timedelta(hours=hh, minutes=mm, seconds=ss, milliseconds=tenths * 100)


def parse_glm_lcfa(
    content: bytes,
    bbox: tuple[float, float, float, float] | None = None,
    t0: datetime | None = None,
    t1: datetime | None = None,
    good_only: bool = True,
) -> list[Flash]:
    """Flashes in one LCFA NetCDF inside *bbox* and ``[t0, t1]``.

    *bbox* is ``(lat_min, lon_min, lat_max, lon_max)``. ``good_only`` keeps only
    ``flash_quality_flag == 0`` (good). Pure (no network) so it is unit-testable
    against a synthetic NetCDF.
    """
    import netCDF4  # type: ignore  # noqa: PLC0415

    lat_min, lon_min, lat_max, lon_max = bbox if bbox else (-90.0, -180.0, 90.0, 180.0)
    try:
        ds = netCDF4.Dataset("inmem.nc", mode="r", memory=content)
    except (ValueError, OSError):
        # Older libnetcdf without in-memory read: spill to a temp file.
        import tempfile  # noqa: PLC0415

        with tempfile.NamedTemporaryFile(suffix=".nc") as fh:
            fh.write(content)
            fh.flush()
            return _parse_open(netCDF4.Dataset(fh.name, mode="r"),
                               (lat_min, lon_min, lat_max, lon_max), t0, t1, good_only)
    try:
        return _parse_open(ds, (lat_min, lon_min, lat_max, lon_max), t0, t1, good_only)
    finally:
        ds.close()


def _parse_open(ds, bbox, t0, t1, good_only) -> list[Flash]:
    import numpy as np  # noqa: PLC0415

    lat_min, lon_min, lat_max, lon_max = bbox
    if "flash_lat" not in ds.variables or "flash_lon" not in ds.variables:
        return []

    def col(name, default=0.0):
        v = ds.variables.get(name)
        if v is None:
            return None
        return np.ma.asarray(v[:]).astype("float64").filled(np.nan)

    lat = col("flash_lat")
    lon = col("flash_lon")
    if lat is None or lon is None or lat.size == 0:
        return []
    n = lat.size
    off = col("flash_time_offset_of_first_event")
    energy = col("flash_energy")
    qflag = col("flash_quality_flag")
    if off is None:
        off = np.zeros(n)
    if energy is None:
        energy = np.zeros(n)
    if qflag is None:
        qflag = np.zeros(n)
    ptime = float(np.ma.asarray(ds.variables["product_time"][:]).reshape(-1)[0])
    base = GLM_EPOCH.timestamp() + ptime
    secs = base + off

    mask = (
        np.isfinite(lat) & np.isfinite(lon)
        & (lat >= lat_min) & (lat <= lat_max)
        & (lon >= lon_min) & (lon <= lon_max)
    )
    if good_only:
        mask &= (qflag == 0)
    if t0 is not None:
        mask &= (secs >= t0.timestamp())
    if t1 is not None:
        mask &= (secs <= t1.timestamp())

    idx = np.nonzero(mask)[0]
    out: list[Flash] = []
    for i in idx:
        out.append(Flash(
            lat=float(lat[i]), lon=float(lon[i]),
            time=datetime.fromtimestamp(float(secs[i]), tz=timezone.utc),
            energy=float(energy[i]),
        ))
    return out


class GLMSource:
    """GLM flashes for a warning's window, from the GOES-East ``LCFA`` archive.

    Lists every hour-prefix spanning ``[start, end]``, downloads the per-file
    NetCDFs concurrently with an unsigned boto3 client, parses each, and returns
    the flashes inside *bbox* and the window. ``boto3``/``netCDF4`` are the
    ``live`` extra. Sub-step lines go to an optional status callback for the GUI.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
        bucket: str | None = None,
        max_flashes: int = 5000,
        good_only: bool = True,
        download_workers: int = 4,
    ) -> None:
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.bbox = bbox
        self.bucket = bucket or bucket_for_date(self.start.date())[0]
        self.max_flashes = max(1, int(max_flashes))
        self.good_only = good_only
        self.download_workers = max(1, int(download_workers))
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

    def _hour_prefixes(self) -> Iterable[str]:
        """``GLM-L2-LCFA/YYYY/DDD/HH/`` for every hour spanning the window."""
        cur = self.start.replace(minute=0, second=0, microsecond=0)
        while cur <= self.end:
            doy = cur.timetuple().tm_yday
            yield f"GLM-L2-LCFA/{cur.year}/{doy:03d}/{cur.hour:02d}/"
            cur += timedelta(hours=1)

    def _list_keys(self, s3) -> list[str]:
        paginator = s3.get_paginator("list_objects_v2")
        keys: list[str] = []
        for prefix in self._hour_prefixes():
            self._emit(f"Listing {self.bucket} {prefix}…")
            try:
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        t = _name_time(key.rsplit("/", 1)[-1])
                        if t is not None and self.start <= t <= self.end:
                            keys.append(key)
            except Exception as e:  # noqa: BLE001 - skip a missing/failed hour
                log.info("glm.hour_skipped", prefix=prefix,
                         reason=str(e).splitlines()[0])
        keys.sort()
        return keys

    def flashes(self, cancel: threading.Event | None = None) -> list[Flash]:
        """All matching flashes in the window, capped at ``max_flashes``."""
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        cancel = cancel or threading.Event()
        s3 = self._client()
        keys = self._list_keys(s3)
        n = len(keys)
        log.info("glm.window", bucket=self.bucket, files=n,
                 start=self.start.isoformat(), end=self.end.isoformat())
        if n == 0:
            self._emit("No GLM files in this window (GLM coverage begins 2017).")
            return []
        self._emit(f"Found {n} GLM file(s); downloading "
                   f"({self.download_workers} at a time)…")

        def fetch(key: str) -> bytes | None:
            if cancel.is_set():
                return None
            try:
                return s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception as e:  # noqa: BLE001 - skip a bad object
                log.info("glm.get_skipped", key=key, reason=str(e).splitlines()[0])
                return None

        out: list[Flash] = []
        done = 0
        with ThreadPoolExecutor(max_workers=self.download_workers,
                                thread_name_prefix="glmdl") as ex:
            for content in ex.map(fetch, keys):
                done += 1
                if cancel.is_set():
                    break
                if content:
                    try:
                        out.extend(parse_glm_lcfa(
                            content, self.bbox, self.start, self.end, self.good_only))
                    except Exception as e:  # noqa: BLE001 - skip a bad file
                        log.info("glm.parse_skipped", reason=str(e).splitlines()[0])
                if done % 10 == 0 or done == n:
                    self._emit(f"Parsed {done}/{n} files — {len(out)} flash(es)…")

        out.sort(key=lambda f: f.time)
        if len(out) > self.max_flashes:
            step = len(out) / self.max_flashes
            sampled = [out[int(i * step)] for i in range(self.max_flashes)]
            self._emit(f"{len(out)} flashes — showing {len(sampled)} "
                       f"(capped at {self.max_flashes}, evenly subsampled).")
            log.info("glm.capped", total=len(out), shown=len(sampled),
                     cap=self.max_flashes)
            out = sampled
        else:
            self._emit(f"{len(out)} flash(es) in window.")
        return out
