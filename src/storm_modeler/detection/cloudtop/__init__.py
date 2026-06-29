"""cloudtop — GOES ABI Band-13 cloud-top identification & tracking.

Mirrors the SCIT (``detection_v2``) data flow for satellite scenes, taking an
explicit :class:`CloudTopParams` — no module-level tunables, no globals:

    run(scene, params)              -> list[CloudTopCell]   # identify one scene
    Tracker(params).update(..)      -> list[CloudTopCell]   # assign track ids
    associate_radar(cells, radar)   -> list[CloudTopCell]   # measure storm tilt
    CloudTopCell                                            # output type
"""

from __future__ import annotations

from ...models import SatelliteScene
from ...settings.resolver import CloudTopParams
from .associate import associate_radar
from .identify import identify
from .track import Tracker
from .types import CloudTopCell

__all__ = [
    "run",
    "identify",
    "Tracker",
    "associate_radar",
    "CloudTopCell",
    "CloudTopParams",
]


def run(
    scene: SatelliteScene, params: CloudTopParams | None = None
) -> list[CloudTopCell]:
    """Identify cold cloud-top cells in one ABI scene (no tracking)."""
    return identify(scene, params)
