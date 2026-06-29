"""GOES ABI Band-13 cloud-top ingest (anonymous NOAA S3).

The ABI Cloud & Moisture Imagery product (``ABI-L2-CMIPC``, CONUS sector,
5-minute cadence) carries calibrated brightness temperature per band; band 13
(10.3 µm, "Clean IR") is the cloud-top channel. The files are mirrored on AWS in
the ``noaa-goes16``/``noaa-goes18``/``noaa-goes19`` buckets under
``ABI-L2-CMIPC/<YYYY>/<DDD>/<HH>/`` — one per scan. We list the hours spanning a
warning's data window, download the per-scene NetCDFs concurrently (unsigned
boto3, the same fast path as the NEXRAD/GLM pulls), open each with xarray, and
**reproject** the brightness temperature from the ABI fixed grid (geostationary
scan angles) onto a regular lon/lat raster cropped to the warning bbox.

The reprojection uses the GOES-R fixed-grid geolocation formula in pure NumPy
(no pyproj) so it is safe to run on a worker thread — building pyproj
transformers off the GUI thread segfaults in PROJ (see ``models.PROJ_LOCK``).

``boto3``/``xarray``/``goes2go`` ride in the ``live`` extra and are imported
lazily, so the offline pipeline never needs them. This module does no Qt and no
GUI work — :class:`storm_modeler.workers.SatelliteWorker` drives it off the GUI
thread.

NOTE: ``goes2go`` is installed for its geolocation/RGB helpers, but the proven
unsigned-boto3 list+download path is used here; the exact goes2go API should be
confirmed if/when its helpers are wired in.
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

import numpy as np
import structlog

from ..models import SatelliteScene

log = structlog.get_logger(__name__)

# GOES imagery lives in us-east-1, public (unsigned) reads.
S3_REGION = "us-east-1"

#: GOES-19 replaced GOES-16 as operational GOES-East on this date.
GOES19_OPERATIONAL = date(2025, 4, 4)

#: Longitude at/west of which CONUS warnings are better covered by GOES-West.
GOES_WEST_LON_CUT = -113.0

#: ``OR_ABI-L2-CMIPC-M6C13_G19_sYYYYDDDHHMMSSt_e..._c....nc`` — band-13 CONUS
#: CMIP. The ``s`` (start) stamp is year(4) doy(3) hour(2) min(2) sec(2) tenths(1).
_CMIP_C13 = re.compile(
    r"OR_ABI-L2-CMIPC-M\dC13_G\d{2}_s(\d{14})_e\d{14}_c\d{14}\.nc$"
)


def satellite_for(lon: float, when: date) -> tuple[str, str, str]:
    """(bucket, label, short) for the satellite covering ``lon`` on ``when``.

    West of :data:`GOES_WEST_LON_CUT` → GOES-West (G18); otherwise GOES-East,
    which is G19 once operational, else G16.
    """
    if lon <= GOES_WEST_LON_CUT:
        return "noaa-goes18", "GOES-18 (West)", "GOES-18"
    if when >= GOES19_OPERATIONAL:
        return "noaa-goes19", "GOES-19 (East)", "GOES-19"
    return "noaa-goes16", "GOES-16 (East)", "GOES-16"


def _name_time(name: str) -> datetime | None:
    """Parse a CMIP filename's start stamp to a UTC datetime (or ``None``)."""
    m = _CMIP_C13.search(name)
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


def geodetic_to_scan_angle(
    lon: np.ndarray,
    lat: np.ndarray,
    lon_origin_deg: float,
    h_m: float,
    r_eq: float,
    r_pol: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """GOES-R geodetic → ABI fixed-grid scan angles (E/W ``x``, N/S ``y``).

    Pure NumPy implementation of the PUG L2 navigation formula. ``h_m`` is the
    distance from Earth centre to satellite (perspective_point_height + r_eq).
    Returns ``(x, y, visible)`` with ``x``/``y`` in radians and a boolean mask
    that is False where the point is over the Earth's limb (not imaged).
    """
    lam = np.radians(lon)
    phi = np.radians(lat)
    lam0 = np.radians(lon_origin_deg)
    req2_rpol2 = (r_eq * r_eq) / (r_pol * r_pol)
    ecc2 = (r_eq * r_eq - r_pol * r_pol) / (r_eq * r_eq)

    phi_c = np.arctan((r_pol * r_pol) / (r_eq * r_eq) * np.tan(phi))
    rc = r_pol / np.sqrt(1.0 - ecc2 * np.cos(phi_c) ** 2)
    sx = h_m - rc * np.cos(phi_c) * np.cos(lam - lam0)
    sy = -rc * np.cos(phi_c) * np.sin(lam - lam0)
    sz = rc * np.sin(phi_c)

    # Visibility: the line of sight must not pass through the Earth.
    visible = h_m * (h_m - sx) >= (sy * sy + req2_rpol2 * sz * sz)
    r = np.sqrt(sx * sx + sy * sy + sz * sz)
    y = np.arctan2(sz, sx)
    x = np.arcsin(np.clip(-sy / r, -1.0, 1.0))
    return x, y, visible


def reproject_cmi(
    cmi: np.ndarray,
    scan_x: np.ndarray,
    scan_y: np.ndarray,
    proj: dict,
    bbox: tuple[float, float, float, float],
    res_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Resample an ABI fixed-grid CMI field onto a regular lon/lat raster.

    ``scan_x``/``scan_y`` are the 1D fixed-grid scan-angle coordinates (radians);
    ``proj`` holds the ``goes_imager_projection`` parameters. Returns
    ``(bt, lons, lats)`` over ``bbox`` at ``res_deg`` spacing, ``lats``
    descending (row 0 = north), NaN off-limb. The target grid is small, so each
    target pixel is inverse-mapped to a scan angle and the source grid is
    sampled there (no per-source-pixel geolocation needed).
    """
    from scipy.interpolate import RegularGridInterpolator

    lon_min, lat_min, lon_max, lat_max = bbox
    lons = np.arange(lon_min, lon_max + res_deg / 2, res_deg, dtype=np.float64)
    lats = np.arange(lat_max, lat_min - res_deg / 2, -res_deg, dtype=np.float64)
    LON, LAT = np.meshgrid(lons, lats)

    x_q, y_q, visible = geodetic_to_scan_angle(
        LON, LAT,
        proj["lon_origin"], proj["h_m"], proj["r_eq"], proj["r_pol"],
    )

    # scan_y is typically descending (north→south); RegularGridInterpolator
    # needs ascending axes, so flip each axis (and the data) as required.
    ys, cmi_y = (scan_y[::-1], cmi[::-1, :]) if scan_y[0] > scan_y[-1] else (scan_y, cmi)
    xs, cmi_yx = (scan_x[::-1], cmi_y[:, ::-1]) if scan_x[0] > scan_x[-1] else (scan_x, cmi_y)

    interp = RegularGridInterpolator(
        (ys, xs), cmi_yx, bounds_error=False, fill_value=np.nan
    )
    sampled = interp(np.column_stack([y_q.ravel(), x_q.ravel()]))
    bt = sampled.reshape(LAT.shape)
    bt = np.where(visible, bt, np.nan).astype(np.float32)
    return bt, lons, lats


def scene_from_dataset(
    ds,
    satellite: str,
    valid_time: datetime,
    bbox: tuple[float, float, float, float],
    res_deg: float,
    band: int = 13,
) -> SatelliteScene:
    """Build a :class:`SatelliteScene` from an open ABI CMIP xarray dataset."""
    cmi = np.asarray(ds["CMI"].values, dtype=np.float32)
    scan_x = np.asarray(ds["x"].values, dtype=np.float64)
    scan_y = np.asarray(ds["y"].values, dtype=np.float64)
    gip = ds["goes_imager_projection"].attrs
    proj = {
        "lon_origin": float(gip["longitude_of_projection_origin"]),
        "h_m": float(gip["perspective_point_height"]) + float(gip["semi_major_axis"]),
        "r_eq": float(gip["semi_major_axis"]),
        "r_pol": float(gip["semi_minor_axis"]),
    }
    bt, lons, lats = reproject_cmi(cmi, scan_x, scan_y, proj, bbox, res_deg)
    return SatelliteScene(
        satellite=satellite, band=band, valid_time=valid_time,
        bt=bt, lons=lons, lats=lats, bbox=bbox,
    )


class ABISource:
    """ABI Band-13 cloud-top scenes for a warning's window, reprojected to lon/lat.

    Lists every hour-prefix spanning ``[start, end]``, downloads the per-scene
    CMIP NetCDFs concurrently with an unsigned boto3 client, opens each with
    xarray, and reprojects the brightness temperature onto a regular lon/lat
    raster over ``bbox``. ``boto3``/``xarray`` are the ``live`` extra. Sub-step
    lines go to an optional status callback for the GUI.
    """

    def __init__(
        self,
        start: datetime,
        end: datetime,
        bbox: tuple[float, float, float, float],
        satellite: str | None = None,
        bucket: str | None = None,
        band: int = 13,
        target_res_deg: float = 0.02,
        download_workers: int = 4,
    ) -> None:
        self.start = start.astimezone(timezone.utc)
        self.end = end.astimezone(timezone.utc)
        self.bbox = bbox
        lon_c = 0.5 * (bbox[0] + bbox[2])
        auto_bucket, auto_label, auto_short = satellite_for(lon_c, self.start.date())
        self.bucket = bucket or auto_bucket
        self.satellite = satellite or auto_short
        self.band = int(band)
        self.target_res_deg = float(target_res_deg)
        self.download_workers = max(1, int(download_workers))
        self._keys: list[tuple[datetime, str]] | None = None
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
        cur = self.start.replace(minute=0, second=0, microsecond=0)
        while cur <= self.end:
            doy = cur.timetuple().tm_yday
            yield f"ABI-L2-CMIPC/{cur.year}/{doy:03d}/{cur.hour:02d}/"
            cur += timedelta(hours=1)

    def _list_keys(self) -> list[tuple[datetime, str]]:
        s3 = self._client()
        paginator = s3.get_paginator("list_objects_v2")
        keys: list[tuple[datetime, str]] = []
        for prefix in self._hour_prefixes():
            self._emit(f"Listing {self.bucket} {prefix}…")
            try:
                for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        t = _name_time(key.rsplit("/", 1)[-1])
                        if t is not None and self.start <= t <= self.end:
                            keys.append((t, key))
            except Exception as e:  # noqa: BLE001 - skip a missing/failed hour
                log.info("abi.hour_skipped", prefix=prefix,
                         reason=str(e).splitlines()[0])
        keys.sort(key=lambda p: p[0])
        return keys

    def _keys_cached(self) -> list[tuple[datetime, str]]:
        if self._keys is None:
            self._keys = self._list_keys()
        return self._keys

    def estimated_count(self) -> int:
        return len(self._keys_cached())

    @staticmethod
    def _open_dataset(content: bytes):
        """Open an in-memory ABI NetCDF as an xarray dataset (temp-file fallback)."""
        import io  # noqa: PLC0415

        import xarray as xr  # type: ignore  # noqa: PLC0415

        try:
            return xr.open_dataset(io.BytesIO(content), engine="h5netcdf")
        except (ValueError, OSError):
            import tempfile  # noqa: PLC0415

            tmp = tempfile.NamedTemporaryFile(suffix=".nc", delete=False)
            tmp.write(content)
            tmp.flush()
            tmp.close()
            return xr.open_dataset(tmp.name)

    def scenes(self, cancel: threading.Event | None = None) -> Iterator[SatelliteScene]:
        """Reprojected scenes in the window, oldest→newest."""
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        cancel = cancel or threading.Event()
        keys = self._keys_cached()
        n = len(keys)
        log.info("abi.window", bucket=self.bucket, satellite=self.satellite, files=n,
                 start=self.start.isoformat(), end=self.end.isoformat())
        if n == 0:
            self._emit("No ABI CMIP scenes in this window.")
            return
        self._emit(f"Found {n} ABI scene(s); downloading "
                   f"({self.download_workers} at a time)…")

        s3 = self._client()  # boto3 low-level clients are thread-safe to share
        items = list(enumerate(keys, 1))  # [(i, (t, key)), …]

        def fetch(item) -> tuple[datetime, bytes | None]:
            i, (t, key) = item
            if cancel.is_set():
                return t, None
            try:
                return t, s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception as e:  # noqa: BLE001 - skip a bad object
                log.info("abi.get_skipped", key=key, reason=str(e).splitlines()[0])
                return t, None

        # Download concurrently with a bounded look-ahead; reproject + yield in
        # strict order (cloud-top tracking downstream is stateful, order-sensitive).
        window = self.download_workers + 2
        done = 0
        with ThreadPoolExecutor(max_workers=self.download_workers,
                                thread_name_prefix="abidl") as ex:
            futures: dict[int, object] = {}
            next_i = 0
            while next_i < min(window, len(items)):
                futures[next_i] = ex.submit(fetch, items[next_i])
                next_i += 1
            for j in range(len(items)):
                t, content = futures.pop(j).result()
                if next_i < len(items):
                    futures[next_i] = ex.submit(fetch, items[next_i])
                    next_i += 1
                done += 1
                if cancel.is_set():
                    break
                if not content:
                    continue
                try:
                    ds = self._open_dataset(content)
                    try:
                        scene = scene_from_dataset(
                            ds, self.satellite, t, self.bbox,
                            self.target_res_deg, self.band,
                        )
                    finally:
                        ds.close()
                except Exception as e:  # noqa: BLE001 - skip a bad file
                    log.info("abi.parse_skipped", reason=str(e).splitlines()[0])
                    continue
                self._emit(f"Reprojected {done}/{n}   {t:%H:%MZ}")
                yield scene

    def __iter__(self) -> Iterator[SatelliteScene]:
        return self.scenes()


class FixtureSceneSource:
    """Read pre-reprojected ``*.npz`` scenes from a fixture's ``scenes/`` dir."""

    def __init__(self, fixture_dir: str | Path) -> None:
        self.scenes_dir = Path(fixture_dir) / "scenes"

    def set_status_callback(self, cb) -> None:  # parity with ABISource
        pass

    def estimated_count(self) -> int:
        return len(list(self.scenes_dir.glob("*.npz")))

    def scenes(self, cancel: threading.Event | None = None) -> Iterator[SatelliteScene]:
        files = sorted(self.scenes_dir.glob("*.npz"))
        out = [SatelliteScene.load_npz(f) for f in files]
        out.sort(key=lambda s: s.valid_time)
        for s in out:
            if cancel is not None and cancel.is_set():
                break
            yield s

    def __iter__(self) -> Iterator[SatelliteScene]:
        return self.scenes()
