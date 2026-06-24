"""On-demand Cartesian grids for a warning's volumes, with an LRU cache.

The 3D model pane scrubs across a warning's volumes; each frame needs that
volume's gridded reflectivity ``Z[z,y,x]`` plus its geo transform. Re-gridding
an archived Level II file is expensive, so the provider:

* serves any grid the pipeline already produced via :meth:`register` (the GUI
  hands over the :class:`GriddedVolume` it just processed — no re-download);
* falls back to a :class:`~storm_modeler.data.volumes.VolumeSource` factory to
  (re-)grid on a cache miss (e.g. a scrubbed frame that was evicted);
* keeps at most ``cache_size`` grids in memory (LRU), so a long event does not
  grow without bound.

This is pure: no Qt, no persistence. The pane drives it from a ``QThreadPool``
worker and hands the resulting actors back to the GUI thread (Section 7).
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime
from typing import Callable, Iterable

import structlog

from ..models import GriddedVolume

log = structlog.get_logger(__name__)

SourceFactory = Callable[[], Iterable[GriddedVolume]]


def _key(t: datetime) -> str:
    return t.isoformat()


class GridProvider:
    """LRU-cached access to a warning's gridded volumes, keyed by ``valid_time``.

    ``source_factory`` (optional) returns a fresh iterable of
    :class:`GriddedVolume` for the warning — used only to recover a grid that is
    not in the cache. In the common GUI path every grid is :meth:`register`-ed
    as the pipeline produces it, so the factory is never invoked.
    """

    def __init__(
        self, source_factory: SourceFactory | None = None, cache_size: int = 8
    ) -> None:
        self.source_factory = source_factory
        self.cache_size = max(1, int(cache_size))
        self._cache: "OrderedDict[str, GriddedVolume]" = OrderedDict()
        self._times: list[datetime] = []  # known order, oldest -> newest

    # --- registration / index --------------------------------------------

    def register(self, volume: GriddedVolume) -> None:
        """Cache an already-gridded volume (most-recently-used)."""
        k = _key(volume.valid_time)
        self._cache[k] = volume
        self._cache.move_to_end(k)
        if volume.valid_time not in self._times:
            self._times.append(volume.valid_time)
            self._times.sort()
        self._evict()

    def register_all(self, volumes: Iterable[GriddedVolume]) -> None:
        for v in volumes:
            self.register(v)

    def set_times(self, times: Iterable[datetime]) -> None:
        """Declare the full ordered set of valid times (for the scrubber).

        Times not yet cached are recovered lazily via the source factory.
        """
        merged = set(self._times) | set(times)
        self._times = sorted(merged)

    def times(self) -> list[datetime]:
        return list(self._times)

    def __len__(self) -> int:
        return len(self._times)

    # --- access -----------------------------------------------------------

    def get(self, valid_time: datetime) -> GriddedVolume:
        """Return the grid for ``valid_time`` (cache, else re-grid via factory)."""
        k = _key(valid_time)
        hit = self._cache.get(k)
        if hit is not None:
            self._cache.move_to_end(k)
            return hit
        if self.source_factory is None:
            raise KeyError(f"no grid cached for {k} and no source factory")
        log.info("grid_provider.regrid", valid_time=k)
        found: GriddedVolume | None = None
        for vol in self.source_factory():
            self.register(vol)  # opportunistically warm the cache
            if _key(vol.valid_time) == k:
                found = self._cache[k]
        if found is None:
            raise KeyError(f"valid_time {k} not produced by the source")
        return found

    def get_index(self, i: int) -> GriddedVolume:
        return self.get(self._times[i])

    def index_of(self, valid_time: datetime) -> int:
        """Index of ``valid_time`` in the ordered times (nearest if not exact)."""
        if valid_time in self._times:
            return self._times.index(valid_time)
        # Fall back to the temporally nearest known time.
        return min(
            range(len(self._times)),
            key=lambda i: abs((self._times[i] - valid_time).total_seconds()),
            default=0,
        )

    # --- internals --------------------------------------------------------

    def _evict(self) -> None:
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)  # drop least-recently-used
