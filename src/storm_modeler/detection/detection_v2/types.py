"""Output types for the SCIT package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from shapely.geometry import Polygon, mapping


@dataclass
class StormCell:
    """A single SCIT-identified storm cell within one volume.

    Attributes mirror the spec's cell contract (envelope polygon, seed,
    ``max_dbz``, ``area_km2``, ``echo_top_km``, ``depth_km``) plus the
    tracking identity (``track_id``) assigned across volumes.
    """

    cell_id: int  # sequential within the volume
    site: str
    valid_time: datetime
    seed_lon: float
    seed_lat: float
    seed_x: float  # metres east of radar
    seed_y: float  # metres north of radar
    max_dbz: float
    area_km2: float
    echo_top_km: float
    base_km: float
    depth_km: float
    n_levels: int
    envelope: Polygon  # convex-hull footprint in lon/lat
    track_id: int = -1  # assigned by the tracker; -1 until tracked
    # GOES ABI cloud-top annotation, filled by ``cloudtop.associate_radar`` once
    # a satellite scene is matched to this cell (None/False until then).
    cloud_top_c: float | None = None  # cloud-top temperature, deg C
    overshooting_top: bool = False  # the matched cloud top overshoots its anvil

    def summary(self) -> str:
        return (
            f"id {self.cell_id}  {self.max_dbz:.1f} dBZ  "
            f"depth {self.depth_km:.1f} km"
        )

    def to_dict(self) -> dict:
        return {
            "cell_id": self.cell_id,
            "track_id": self.track_id,
            "site": self.site,
            "valid_time": self.valid_time.isoformat(),
            "seed_lon": self.seed_lon,
            "seed_lat": self.seed_lat,
            "seed_x": self.seed_x,
            "seed_y": self.seed_y,
            "max_dbz": self.max_dbz,
            "area_km2": self.area_km2,
            "echo_top_km": self.echo_top_km,
            "base_km": self.base_km,
            "depth_km": self.depth_km,
            "n_levels": self.n_levels,
            "cloud_top_c": self.cloud_top_c,
            "overshooting_top": self.overshooting_top,
            "envelope": mapping(self.envelope),
        }
