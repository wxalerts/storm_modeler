"""Shared data model.

These dataclasses are the seams between stages: a :class:`Warning` from a
``WarningSource``, a :class:`GriddedVolume` from a ``VolumeSource`` (after
gridding), and the storm cells SCIT emits live in ``detection.detection_v2``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon, mapping, shape


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

    def __post_init__(self) -> None:
        self.valid_time = _parse_dt(self.valid_time)
        self.reflectivity = np.asarray(self.reflectivity, dtype=np.float32)
        self.x = np.asarray(self.x, dtype=np.float64)
        self.y = np.asarray(self.y, dtype=np.float64)
        self.z = np.asarray(self.z, dtype=np.float64)

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
        lon, lat = self.transformer_to_lonlat().transform(xm, ym)
        return lon, lat

    def composite_reflectivity(self) -> np.ndarray:
        """Column-max reflectivity (ny, nx); NaN where the whole column is empty."""
        filled = np.where(np.isnan(self.reflectivity), -np.inf, self.reflectivity)
        comp = filled.max(axis=0)
        return np.where(np.isneginf(comp), np.nan, comp)

    # --- (de)serialisation for fixtures -----------------------------------

    def save_npz(self, path: str | Path) -> None:
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
        )

    @classmethod
    def load_npz(cls, path: str | Path) -> "GriddedVolume":
        d = np.load(path, allow_pickle=False)
        return cls(
            site=str(d["site"]),
            valid_time=str(d["valid_time"]),
            reflectivity=d["reflectivity"],
            x=d["x"],
            y=d["y"],
            z=d["z"],
            lat0=float(d["lat0"]),
            lon0=float(d["lon0"]),
        )
