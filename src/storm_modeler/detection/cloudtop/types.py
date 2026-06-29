"""Output type for the cloud-top detection package."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from shapely.geometry import Polygon, mapping


@dataclass
class CloudTopCell:
    """A single cold cloud-top object within one GOES ABI Band-13 scene.

    The coldest pixel is the overshooting-top candidate (``cold_lon``/``cold_lat``,
    ``min_bt_k``); the brightness-temperature-weighted centroid
    (``centroid_lon``/``centroid_lat``) is the cell's plan-view location. Flags
    mark a large cold anvil canopy (``anviled_out``) and an overshooting top
    (``overshooting_top``). ``radar_track_id``/``tilt_km``/``tilt_bearing_deg``
    are filled by :func:`associate_radar` once the nearest radar cell is known —
    a large tilt means the cloud-top core is displaced from the radar core, i.e.
    a tilted/sheared storm column.
    """

    cell_id: int  # sequential within the scene
    satellite: str
    valid_time: datetime
    cold_lon: float  # coldest pixel (overshooting-top candidate)
    cold_lat: float
    min_bt_k: float
    centroid_lon: float  # BT-weighted centroid
    centroid_lat: float
    mean_bt_k: float
    area_km2: float
    anviled_out: bool
    overshooting_top: bool
    ot_depth_k: float  # anvil-background mean minus coldest pixel
    envelope: Polygon  # convex-hull footprint in lon/lat
    track_id: int = -1  # assigned by the tracker; -1 until tracked
    radar_track_id: int = -1  # nearest radar track; -1 if none within range
    tilt_km: float = float("nan")  # cold-core to radar-core distance
    tilt_bearing_deg: float = float("nan")  # bearing radar-core -> cold-core

    def summary(self) -> str:
        return (
            f"id {self.cell_id}  {self.min_bt_k:.1f} K  "
            f"{self.area_km2:.0f} km^2"
            + ("  OT" if self.overshooting_top else "")
            + ("  anvil" if self.anviled_out else "")
        )

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "track_id": self.track_id,
            "satellite": self.satellite,
            "valid_time": self.valid_time.isoformat(),
            "cold_lon": self.cold_lon,
            "cold_lat": self.cold_lat,
            "min_bt_k": self.min_bt_k,
            "centroid_lon": self.centroid_lon,
            "centroid_lat": self.centroid_lat,
            "mean_bt_k": self.mean_bt_k,
            "area_km2": self.area_km2,
            "anviled_out": self.anviled_out,
            "overshooting_top": self.overshooting_top,
            "ot_depth_k": self.ot_depth_k,
            "radar_track_id": self.radar_track_id,
            "tilt_km": self.tilt_km,
            "tilt_bearing_deg": self.tilt_bearing_deg,
            "envelope": mapping(self.envelope),
        }
