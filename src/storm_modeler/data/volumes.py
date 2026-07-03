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

    #: Optional sink for human-readable, sub-step status lines ("Downloading
    #: … 3.1/5.4 MB", "Gridding …"). The GUI wires this to the progress dialog
    #: so long-running steps show motion; headless callers leave it unset.
    _status_cb = None

    def set_status_callback(self, cb) -> None:
        """Register a ``cb(message: str)`` to receive sub-step status lines."""
        self._status_cb = cb

    def _emit_status(self, message: str) -> None:
        if self._status_cb is not None:
            self._status_cb(message)

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


def _velocity_range_folded(file_obj, nrays: int, max_ngates: int):
    """Per-gate range-folded mask for the VEL moment, aligned to the Radar rows.

    NEXRAD Level II encodes raw moment value 0 as below-threshold and 1 as
    **range folded**; Py-ART masks both away (``data <= 1``), so the RF signal
    never reaches the Radar object. The velocity display wants it as its own
    color (AWIPS ``Vel.cmap`` index 1) — RF gates beside a couplet are
    themselves a signal that the velocities there are extreme — so re-decode
    the archive bytes and keep ``raw == 1`` within each ray's real gate count
    (beyond it, 1 is only ``get_data``'s pad value). The second decode costs
    a couple of seconds, small next to Barnes gridding. Returns a
    ``(nrays, max_ngates)`` bool array, or ``None`` when the file can't be
    re-read or the ray count doesn't line up (e.g. legacy volumes whose gate
    spacing Py-ART interpolates — those are left without an RF layer).
    """
    import numpy as np
    from pyart.io.nexrad_level2 import NEXRADLevel2File  # type: ignore

    try:
        file_obj.seek(0)
        nfile = NEXRADLevel2File(file_obj)
        msg_nums = nfile._msg_nums(range(nfile.nscans))
        if len(msg_nums) != nrays:
            log.info("grid.rf_skipped", reason="ray count mismatch",
                     rays=len(msg_nums), expected=nrays)
            return None
        rf = np.zeros((nrays, max_ngates), dtype=bool)
        for i, msg_num in enumerate(msg_nums):
            msg = nfile.radial_records[msg_num]
            if "VEL" not in msg.keys():
                continue
            ng = min(msg["VEL"]["ngates"], max_ngates, len(msg["VEL"]["data"]))
            rf[i, :ng] = np.asarray(msg["VEL"]["data"][:ng]) == 1
        return rf
    except Exception as e:  # noqa: BLE001 - RF is an enhancement, never fatal
        log.info("grid.rf_skipped", reason=str(e).splitlines()[0])
        return None


def grid_level2(
    file_obj,
    site: str,
    lat0: float,
    lon0: float,
    grid: GridConfig = DEFAULT_GRID,
    h_km: float = 1.0,
    v_km: float = 0.5,
    on_status=None,
) -> GriddedVolume:
    """Grid one archived Level II volume onto the local Cartesian grid.

    Uses Py-ART (lazy import). Reflectivity is mapped onto an azimuthal
    -equidistant tangent plane centred on the radar so the result matches the
    :class:`GriddedVolume` contract SCIT consumes. ``on_status(message)``, if
    given, receives a human-readable line per slow phase (decode, interpolate).
    """
    import io
    import time

    import numpy as np
    import pyart  # type: ignore

    def status(msg: str) -> None:
        if on_status is not None:
            on_status(msg)

    status("Decoding radar volume…")
    t0 = time.perf_counter()
    # Buffer the archive bytes: read_nexrad_archive closes the handle it is
    # given, and the range-folded extraction below needs a second pass.
    data = file_obj.read()
    radar = pyart.io.read_nexrad_archive(io.BytesIO(data))
    log.info("grid.read_done", site=site, nrays=int(radar.nrays),
             nsweeps=int(radar.nsweeps), secs=round(time.perf_counter() - t0, 2))

    hw = grid.half_width_km * 1000.0
    nz, ny, nx = grid.nz(v_km), grid.nx(h_km), grid.nx(h_km)
    z_lim = (grid.z_min_km * 1000.0, grid.z_max_km * 1000.0)

    # Grid every dual-pol moment the volume carries (reflectivity in 3D for the
    # 3D pane; the rest are reduced to 2D map layers below to keep the cache small).
    wanted = ["reflectivity", "velocity", "spectrum_width",
              "differential_reflectivity", "cross_correlation_ratio",
              "differential_phase"]
    present = [f for f in wanted if f in radar.fields]

    # Range-folded velocity gates ride along as their own 0/1 field so the
    # velocity display can paint them (AWIPS Vel.cmap index 1) instead of
    # dropping them with the below-threshold gates.
    if "velocity" in present:
        rf = _velocity_range_folded(io.BytesIO(data), int(radar.nrays),
                                    int(radar.ngates))
        if rf is not None:
            radar.add_field(
                "range_folded",
                {"data": rf.astype("float32"), "units": "",
                 "long_name": "velocity range folded flag",
                 "_FillValue": -9999.0},
                replace_existing=True,
            )
            present = present + ["range_folded"]
    del data  # the (possibly large) archive copy is no longer needed

    status(f"Interpolating {len(present)} product(s) to {nz}×{ny}×{nx} grid…")
    log.info("grid.interp_begin", site=site, shape=(nz, ny, nx),
             fields=present, h_km=h_km, v_km=v_km)
    t1 = time.perf_counter()
    gridded = pyart.map.grid_from_radars(
        (radar,),
        grid_shape=(nz, ny, nx),
        grid_limits=(z_lim, (-hw, hw), (-hw, hw)),
        fields=present,
        weighting_function="Barnes2",
    )
    log.info("grid.interp_done", site=site, secs=round(time.perf_counter() - t1, 2))

    def field3d(name: str) -> np.ndarray:
        return np.ma.filled(gridded.fields[name]["data"], np.nan).astype(np.float32)

    def lowest_valid(a: np.ndarray) -> np.ndarray:
        """Per-column value at the lowest grid level that has data (the near-
        surface tilt) — the standard 2D view for velocity and the other moments."""
        valid = np.isfinite(a)
        has = valid.any(axis=0)
        idx = valid.argmax(axis=0)  # first True level per column
        out = np.take_along_axis(a, idx[None], axis=0)[0]
        return np.where(has, out, np.nan)

    refl = field3d("reflectivity")
    products: dict[str, np.ndarray] = {}  # reflectivity composite is added by the model
    for name in present:
        if name in ("reflectivity", "velocity", "range_folded"):
            continue
        products[name] = lowest_valid(field3d(name))

    if "velocity" in present:
        if "range_folded" in present:
            # Lowest level where the radar *observed* the column — valid
            # velocity or range folded. Where that lowest observation is RF,
            # show RF (as the single-tilt NWS display would) instead of
            # silently substituting a higher tilt's velocity.
            vel3 = field3d("velocity")
            rf3 = np.nan_to_num(field3d("range_folded"), nan=0.0) >= 0.5
            observed = np.isfinite(vel3) | rf3
            has = observed.any(axis=0)
            idx = observed.argmax(axis=0)  # first observed level per column
            vel2 = np.take_along_axis(vel3, idx[None], axis=0)[0]
            rf2 = np.take_along_axis(rf3, idx[None], axis=0)[0] & has
            products["velocity"] = np.where(has & ~rf2, vel2, np.nan)
            products["range_folded"] = rf2.astype(np.float32)
        else:
            products["velocity"] = lowest_valid(field3d("velocity"))

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
        products=products,
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
            self._emit_status(f"Listing {self.site} catalog for {day:%Y-%m-%d}…")
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
        n = len(keys)
        log.info("thredds.window", site=self.site, n=n,
                 start=self.start.isoformat(), end=self.end.isoformat())
        self._emit_status(f"Found {n} volume(s) in window.")
        with httpx.Client(timeout=180, follow_redirects=True) as client:
            for i, (t, path) in enumerate(keys, 1):
                name = path.rsplit("/", 1)[-1]
                url = f"{THREDDS_FILESERVER}/{path}"
                log.info("thredds.download_begin", index=i, total=n,
                         valid_time=t.isoformat(), file=name)
                t0 = time.perf_counter()
                content = self._download(client, url, name, i, n)
                mb = len(content) / 1e6
                log.info("thredds.download_done", index=i, total=n,
                         mb=round(mb, 2), secs=round(time.perf_counter() - t0, 2))
                log.info("thredds.grid_begin", index=i, total=n)
                t1 = time.perf_counter()
                vol = grid_level2(
                    io.BytesIO(content), self.site, self.lat0, self.lon0,
                    self.grid, self.h_km, self.v_km,
                    on_status=lambda m, i=i, n=n: self._emit_status(f"[{i}/{n}] {m}"),
                )
                log.info("thredds.grid_done", index=i, total=n,
                         valid_time=vol.valid_time.isoformat(),
                         secs=round(time.perf_counter() - t1, 2))
                yield vol

    def _download(self, client, url: str, name: str, index: int, total: int) -> bytes:
        """Stream one ``.ar2v`` to memory, emitting MB-progress as it arrives."""
        chunks: list[bytes] = []
        got = 0
        last_emit = 0.0
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("Content-Length", 0))
            size_mb = size / 1e6
            for chunk in resp.iter_bytes(chunk_size=256 * 1024):
                chunks.append(chunk)
                got += len(chunk)
                got_mb = got / 1e6
                # Throttle to ~every 0.5 MB so we don't flood the GUI queue.
                if got_mb - last_emit >= 0.5 or got == size:
                    last_emit = got_mb
                    tail = f"{got_mb:.1f}/{size_mb:.1f} MB" if size else f"{got_mb:.1f} MB"
                    self._emit_status(f"[{index}/{total}] Downloading {name}  {tail}")
        return b"".join(chunks)


# --- Unidata anonymous S3 (fast path) --------------------------------------

#: Unidata's real-time Level II mirror on AWS S3. Anonymous (unsigned) reads
#: are ~30x faster than the THREDDS HTTP fileServer in practice, and boto3's
#: transfer manager parallelises the GET. ``us-east-1`` is where the bucket
#: lives. (Same source the WxAlerts nexrad-proc service uses.)
S3_BUCKET = "unidata-nexrad-level2"
S3_REGION = "us-east-1"
#: ``KRAX20260628_171035_V06`` / ``..._V06.bz2`` -> the HHMMSS group.
_S3_NAME = re.compile(r"^[A-Z]{4}(\d{8})_(\d{6})_V0\d(?:\.bz2)?$")


class S3Level2Source(VolumeSource):
    """Archived Level II volumes for ``site`` in ``[start, end]`` via Unidata S3.

    Lists ``<YYYY/MM/DD>/<SITE>/`` for each day in the window with an anonymous
    (``UNSIGNED``) boto3 client, keeps the volumes whose filename time falls in
    the window, and downloads each with boto3's transfer manager — the fast path
    versus :class:`ThreddsLevel2Source`. ``boto3``/``pyart`` are the ``live``
    extra. Produces the same :class:`GriddedVolume` contract as the other
    sources.
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
        self._keys: list[tuple[datetime, str, int]] | None = None
        # How many volumes to download concurrently. Downloads are I/O-bound and
        # independent, so a small pool fetches ahead while the (slower) gridding
        # runs in order. Override with STORM_MODELER_DL_WORKERS.
        import os
        self.download_workers = max(1, int(os.environ.get("STORM_MODELER_DL_WORKERS", "4")))

    @staticmethod
    def _client():
        import boto3  # type: ignore
        from botocore import UNSIGNED  # type: ignore
        from botocore.config import Config  # type: ignore

        return boto3.client(
            "s3", region_name=S3_REGION, config=Config(signature_version=UNSIGNED)
        )

    @staticmethod
    def _key_time(name: str) -> datetime | None:
        m = _S3_NAME.match(name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None

    def _list_keys(self) -> list[tuple[datetime, str, int]]:
        """(`valid_time`, `key`, `size`) for every volume in the window, sorted."""
        s3 = self._client()
        paginator = s3.get_paginator("list_objects_v2")
        out: list[tuple[datetime, str, int]] = []
        day = self.start.date()
        while day <= self.end.date():
            prefix = f"{day:%Y/%m/%d}/{self.site}/"
            self._emit_status(f"Listing {self.site} S3 for {day:%Y-%m-%d}…")
            try:
                for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
                    for obj in page.get("Contents", []):
                        key = obj["Key"]
                        t = self._key_time(key.rsplit("/", 1)[-1])
                        if t is not None and self.start <= t <= self.end:
                            out.append((t, key, int(obj.get("Size", 0))))
            except Exception as e:  # noqa: BLE001 - skip a missing/failed day
                log.info("s3.day_skipped", day=str(day), reason=str(e).splitlines()[0])
            day += timedelta(days=1)
        out.sort(key=lambda p: p[0])
        return out

    def _keys_cached(self) -> list[tuple[datetime, str, int]]:
        if self._keys is None:
            self._keys = self._list_keys()
        return self._keys

    def estimated_count(self) -> int:
        return len(self._keys_cached())

    def volumes(self) -> Iterator[GriddedVolume]:
        import io
        import threading
        import time
        from concurrent.futures import ThreadPoolExecutor

        keys = self._keys_cached()
        n = len(keys)
        log.info("s3.window", site=self.site, n=n, workers=self.download_workers,
                 start=self.start.isoformat(), end=self.end.isoformat())
        self._emit_status(f"Found {n} volume(s); downloading "
                          f"({self.download_workers} at a time)…")
        if n == 0:
            return

        s3 = self._client()  # boto3 low-level clients are thread-safe to share
        items = list(enumerate(keys, 1))  # [(i, (t, key, size)), …]
        done = {"n": 0}
        lock = threading.Lock()

        def fetch(item) -> bytes:
            i, (t, key, size) = item
            name = key.rsplit("/", 1)[-1]
            log.info("s3.download_begin", index=i, total=n,
                     valid_time=t.isoformat(), file=name, mb=round(size / 1e6, 2))
            t0 = time.perf_counter()
            content = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            with lock:
                done["n"] += 1
                k = done["n"]
            log.info("s3.download_done", index=i, total=n, mb=round(len(content) / 1e6, 2),
                     secs=round(time.perf_counter() - t0, 2))
            self._emit_status(f"Downloaded {k}/{n}   {name}")
            return content

        # Download concurrently with a bounded look-ahead, but grid + yield in
        # strict order — SCIT tracking downstream is stateful and order-sensitive.
        # The pool keeps fetching the next volumes while the current one grids.
        window = self.download_workers + 2
        with ThreadPoolExecutor(max_workers=self.download_workers,
                                thread_name_prefix="s3dl") as ex:
            futures: dict[int, object] = {}
            next_i = 0
            while next_i < min(window, len(items)):
                futures[next_i] = ex.submit(fetch, items[next_i])
                next_i += 1
            for j in range(len(items)):
                i, (t, key, size) = items[j]
                content = futures.pop(j).result()
                if next_i < len(items):  # keep the window full
                    futures[next_i] = ex.submit(fetch, items[next_i])
                    next_i += 1
                log.info("s3.grid_begin", index=i, total=n)
                t1 = time.perf_counter()
                vol = grid_level2(
                    io.BytesIO(content), self.site, self.lat0, self.lon0,
                    self.grid, self.h_km, self.v_km,
                    on_status=lambda m, i=i, n=n: self._emit_status(f"[{i}/{n}] {m}"),
                )
                log.info("s3.grid_done", index=i, total=n,
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
