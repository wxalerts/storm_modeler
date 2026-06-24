"""detection_v2 — the SCIT (Storm Cell Identification & Tracking) package.

The code under test. The harness imports it; it is not reimplemented elsewhere.
SCIT takes an explicit :class:`DetectionParams` — no module-level tunables, no
globals — so it is pure and testable:

    run(volume, params)         -> list[StormCell]   # identify one volume
    Tracker(params).update(..)  -> list[StormCell]   # assign track ids
    StormCell                                         # output type
"""

from __future__ import annotations

from ...models import GriddedVolume
from ...settings.resolver import DetectionParams
from .identify import identify
from .track import Tracker
from .types import StormCell

__all__ = ["run", "identify", "Tracker", "StormCell", "DetectionParams"]


def run(volume: GriddedVolume, params: DetectionParams | None = None) -> list[StormCell]:
    """Identify storm cells in one gridded volume (no tracking).

    One volume + params in → list of cell objects out, exactly as the spec's
    data flow describes (``grid → detection_v2.run(volume, params) → cells``).
    """
    return identify(volume, params)
