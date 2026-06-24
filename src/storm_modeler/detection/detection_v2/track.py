"""SCIT tracking — associate cells across consecutive volumes.

A stateful greedy nearest-neighbour tracker, gated by an absolute displacement
(``track_max_km``) rather than a clock. A track survives up to ``track_miss_max``
unmatched volumes before it is retired, so a cell that briefly drops below seed
strength can be re-acquired. Deterministic given the (deterministic) cell order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from ...settings.resolver import DetectionParams
from .types import StormCell


@dataclass
class _Track:
    track_id: int
    x: float
    y: float
    misses: int = 0


class Tracker:
    def __init__(self, params: DetectionParams | None = None) -> None:
        self.params = params or DetectionParams()
        self._next_track_id = 1
        self._active: list[_Track] = []

    def _new_track(self, c: StormCell) -> int:
        tid = self._next_track_id
        self._next_track_id += 1
        self._active.append(_Track(track_id=tid, x=c.seed_x, y=c.seed_y))
        return tid

    def update(self, cells: list[StormCell], valid_time: datetime | None = None) -> list[StormCell]:
        """Assign ``track_id`` to ``cells`` (mutated in place) and return them."""
        max_dist = self.params.track_max_km * 1000.0  # metres
        claimed: set[int] = set()  # indices into self._active

        for c in cells:  # strongest-first
            best_i, best_d = -1, max_dist
            for i, t in enumerate(self._active):
                if i in claimed:
                    continue
                d = math.hypot(c.seed_x - t.x, c.seed_y - t.y)
                if d <= best_d:
                    best_d, best_i = d, i
            if best_i >= 0:
                t = self._active[best_i]
                c.track_id = t.track_id
                t.x, t.y, t.misses = c.seed_x, c.seed_y, 0
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
