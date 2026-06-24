"""SCIT tracking — associate cells across consecutive volumes.

A stateful greedy nearest-neighbour tracker. Each new cell inherits the
``track_id`` of the closest prior cell within a motion-gated radius
(``max_track_speed_ms × Δt``); unmatched cells start fresh tracks. The
association is deterministic given the (already deterministic) cell ordering.
"""

from __future__ import annotations

import math
from datetime import datetime

from ...config import ScitConfig
from ...models import _parse_dt
from .types import StormCell


class Tracker:
    def __init__(self, config: ScitConfig | None = None) -> None:
        self.config = config or ScitConfig()
        self._next_track_id = 1
        self._prev: list[StormCell] = []
        self._prev_time: datetime | None = None

    def _new_track(self) -> int:
        tid = self._next_track_id
        self._next_track_id += 1
        return tid

    def update(self, cells: list[StormCell], valid_time: datetime) -> list[StormCell]:
        """Assign ``track_id`` to ``cells`` (mutated in place) and return them."""
        valid_time = _parse_dt(valid_time)
        if self._prev_time is None or not self._prev:
            for c in cells:
                c.track_id = self._new_track()
            self._prev = list(cells)
            self._prev_time = valid_time
            return cells

        dt = (valid_time - self._prev_time).total_seconds()
        if dt <= 0:
            dt = 1.0
        max_dist = self.config.max_track_speed_ms * dt  # metres

        taken: set[int] = set()  # indices into self._prev already claimed
        for c in cells:  # cells are pre-sorted strongest-first
            best_j = -1
            best_d = max_dist
            for j, p in enumerate(self._prev):
                if j in taken:
                    continue
                d = math.hypot(c.seed_x - p.seed_x, c.seed_y - p.seed_y)
                if d <= best_d:
                    best_d = d
                    best_j = j
            if best_j >= 0:
                c.track_id = self._prev[best_j].track_id
                taken.add(best_j)
            else:
                c.track_id = self._new_track()

        self._prev = list(cells)
        self._prev_time = valid_time
        return cells
