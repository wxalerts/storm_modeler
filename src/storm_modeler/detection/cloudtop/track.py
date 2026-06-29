"""Cloud-top tracking — associate cells across consecutive ABI scenes.

A stateful greedy nearest-neighbour tracker mirroring
``detection_v2.track.Tracker``, but gated by great-circle distance in km
(cloud-top cells live in lon/lat) rather than projected metres. A track
survives up to ``track_miss_max`` unmatched scenes before it is retired.
Deterministic given the (deterministic) cell order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ...settings.resolver import CloudTopParams
from .geo import haversine_km
from .types import CloudTopCell


@dataclass
class _Track:
    track_id: int
    lon: float
    lat: float
    misses: int = 0


class Tracker:
    def __init__(self, params: CloudTopParams | None = None) -> None:
        self.params = params or CloudTopParams()
        self._next_track_id = 1
        self._active: list[_Track] = []

    def _new_track(self, c: CloudTopCell) -> int:
        tid = self._next_track_id
        self._next_track_id += 1
        self._active.append(_Track(track_id=tid, lon=c.cold_lon, lat=c.cold_lat))
        return tid

    def update(
        self, cells: list[CloudTopCell], valid_time: datetime | None = None
    ) -> list[CloudTopCell]:
        """Assign ``track_id`` to ``cells`` (mutated in place) and return them."""
        max_dist = self.params.track_max_km
        claimed: set[int] = set()  # indices into self._active

        for c in cells:  # coldest-first
            best_i, best_d = -1, max_dist
            for i, t in enumerate(self._active):
                if i in claimed:
                    continue
                d = haversine_km(c.cold_lon, c.cold_lat, t.lon, t.lat)
                if d <= best_d:
                    best_d, best_i = d, i
            if best_i >= 0:
                t = self._active[best_i]
                c.track_id = t.track_id
                t.lon, t.lat, t.misses = c.cold_lon, c.cold_lat, 0
                claimed.add(best_i)
            else:
                c.track_id = self._new_track(c)
                claimed.add(len(self._active) - 1)

        # Age unmatched tracks; retire those over the miss tolerance.
        survivors: list[_Track] = []
        for i, t in enumerate(self._active):
            if i in claimed:
                survivors.append(t)
            else:
                t.misses += 1
                if t.misses <= self.params.track_miss_max:
                    survivors.append(t)
        self._active = survivors
        return cells
