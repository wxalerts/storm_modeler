"""Shared data model.

These dataclasses are the seams between stages: a :class:`Warning` from a
``WarningSource``, a :class:`GriddedVolume` from a ``VolumeSource`` (after
gridding), and the storm cells SCIT emits live in ``detection.detection_v2``.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon, mapping, shape

#: Serialises every pyproj/PROJ operation across threads. PROJ context creation
#: is not concurrency-safe, and during a live download two threads use pyproj at
#: once — the worker detecting a volume (StormCell seed lon/lat) while the GUI
#: thread renders the previous volume's radar/cells. Doing both at the same
#: instant intermittently segfaults in PROJ, so all transforms take this lock.
#: Anything that builds a pyproj Transformer (scene/cross-section too) must use
#: it. The work under the lock is microseconds, so contention is negligible.
PROJ_LOCK = threading.Lock()


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class Warning:
    """A single historical NWS storm-based warning.

    Mirrors the WarningSource contract from the spec:
    ``{id, event, phenomena, significance, wfo, etn, ugc[], states[],
    polygon, issued, expires}``.
    """

    id: str
    event: str
    phenomena: str
    significance: str
    wfo: str
    etn: int
    ugc: list[str]
    states: list[str]
    polygon: Polygon
    issued: datetime
    expires: datetime

    def __post_init__(self) -> None:
        self.issued = _parse_dt(self.issued)
        self.expires = _parse_dt(self.expires)
        if not isinstance(self.polygon, Polygon):
            self.polygon = shape(self.polygon)
        self.ugc = list(self.ugc)
        self.states = list(self.states)
        self.etn = int(self.etn)

    @property
    def centroid(self) -> tuple[float, float]:
        """(lon, lat) of the polygon centroid."""
        c = self.polygon.centroid
        return float(c.x), float(c.y)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["polygon"] = mapping(self.polygon)
        d["issued"] = self.issued.isoformat()
        d["expires"] = self.expires.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Warning":
        return cls(
            id=d["id"],
            event=d["event"],
            phenomena=d["phenomena"],
            significance=d["significance"],
            wfo=d["wfo"],
            etn=d["etn"],
            ugc=d["ugc"],
            states=d["states"],
            polygon=shape(d["polygon"]),
            issued=d["issued"],
            expires=d["expires"],
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "Warning":
        return cls.from_dict(json.loads(Path(path).read_text()))


@dataclass
class GriddedVolume:
    """A single radar volume gridded onto a local Cartesian tangent plane.

    The grid is azimuthal-equidistant, centred on the radar (``lat0``/``lon0``)
    so that ``x`` (east) / ``y`` (north) are metres on the ground and ``z`` is
    height AGL in metres. ``reflectivity`` is dBZ with NaN where no echo.
    This is the *gridded* representation SCIT consumes — produced either by
    Py-ART from an archived Level II file, or read directly from a fixture.
    """

    site: str
    valid_time: datetime
    reflectivity: np.ndarray  # (nz, ny, nx) dBZ, NaN for no-data
    x: np.ndarray  # (nx,) metres east of radar
    y: np.ndarray  # (ny,) metres north of radar
    z: np.ndarray  # (nz,) metres AGL
    lat0: float
    lon0: float
    # 2D (ny, nx) display reductions per radar product, e.g. {"reflectivity":
    # column-max, "velocity": lowest-tilt, …}. Reflectivity is kept in full 3D
    # above (the 3D pane needs it); the other dual-pol moments are stored here
    # as their 2D map layer only, to keep the on-disk cache small.
    products: dict | None = None

    def __post_init__(self) -> None:
        self.valid_time = _parse_dt(self.valid_time)
        self.reflectivity = np.asarray(self.reflectivity, dtype=np.float32)
        self.x = np.asarray(self.x, dtype=np.float64)
        self.y = np.asarray(self.y, dtype=np.float64)
        self.z = np.asarray(self.z, dtype=np.float64)
        self.products = {k: np.asarray(v, dtype=np.float32)
                         for k, v in (self.products or {}).items()}
        # Reflectivity's 2D layer is always its composite (column max).
        self.products.setdefault("reflectivity", self.composite_reflectivity())

    def product_2d(self, name: str) -> np.ndarray | None:
        """The 2D (ny, nx) display layer for ``name`` (None if not available)."""
        return self.products.get(name)

    @property
    def available_products(self) -> list[str]:
        return list(self.products.keys())

    @property
    def shape(self) -> tuple[int, int, int]:
        return self.reflectivity.shape

    @property
    def dz_km(self) -> float:
        if self.z.size < 2:
            return 0.0
        return float(np.mean(np.diff(self.z))) / 1000.0

    @property
    def dx_km(self) -> float:
        if self.x.size < 2:
            return 1.0
        return float(np.mean(np.diff(self.x))) / 1000.0

    def transformer_to_lonlat(self) -> Transformer:
        """A pyproj transformer from local metres (x,y) to (lon, lat)."""
        aeqd = (
            f"+proj=aeqd +lat_0={self.lat0} +lon_0={self.lon0} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
        return Transformer.from_crs(aeqd, "EPSG:4326", always_xy=True)

    def xy_to_lonlat(self, xm: np.ndarray, ym: np.ndarray):
        with PROJ_LOCK:
            lon, lat = self.transformer_to_lonlat().transform(xm, ym)
        return lon, lat

    def transformer_from_lonlat(self) -> Transformer:
        """A pyproj transformer from (lon, lat) to local metres (x,y)."""
        aeqd = (
            f"+proj=aeqd +lat_0={self.lat0} +lon_0={self.lon0} "
            "+x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"
        )
        return Transformer.from_crs("EPSG:4326", aeqd, always_xy=True)

    def lonlat_to_xy(self, lon: np.ndarray, lat: np.ndarray):
        """Inverse of :meth:`xy_to_lonlat` — (lon,lat) back to local metres."""
        with PROJ_LOCK:
            x, y = self.transformer_from_lonlat().transform(lon, lat)
        return x, y

    def composite_reflectivity(self) -> np.ndarray:
        """Column-max reflectivity (ny, nx); NaN where the whole column is empty."""
        filled = np.where(np.isnan(self.reflectivity), -np.inf, self.reflectivity)
        comp = filled.max(axis=0)
        return np.where(np.isneginf(comp), np.nan, comp)

    # --- (de)serialisation for fixtures -----------------------------------

    def save_npz(self, path: str | Path) -> None:
        # Non-reflectivity products are stored as 2D layers under "p_<name>".
        extra = {
            f"p_{k}": v for k, v in self.products.items() if k != "reflectivity"
        }
        np.savez_compressed(
            path,
            site=self.site,
            valid_time=self.valid_time.isoformat(),
            reflectivity=self.reflectivity,
            x=self.x,
            y=self.y,
            z=self.z,
            lat0=self.lat0,
            lon0=self.lon0,
            **extra,
        )

    @classmethod
    def load_npz(cls, path: str | Path) -> "GriddedVolume":
        d = np.load(path, allow_pickle=False)
        products = {k[2:]: d[k] for k in d.files if k.startswith("p_")}
        return cls(
            site=str(d["site"]),
            valid_time=str(d["valid_time"]),
            reflectivity=d["reflectivity"],
            x=d["x"],
            y=d["y"],
            z=d["z"],
            lat0=float(d["lat0"]),
            lon0=float(d["lon0"]),
            products=products,
        )
