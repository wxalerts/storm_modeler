"""detection_v2 — the SCIT (Storm Cell Identification & Tracking) package.

This is the code under test. The harness imports it; it is not reimplemented
elsewhere. The public surface is intentionally tiny:

    run(volume, config)        -> list[StormCell]   # identify one volume
    Tracker(config).update(..) -> list[StormCell]   # assign track ids
    StormCell                                        # output type

``run`` returns identified, admitted cells for a single gridded volume. The
:class:`Tracker` carries cross-volume state to assign stable ``track_id``s.
"""

from __future__ import annotations

from ...config import ScitConfig
from ...models import GriddedVolume
from .identify import identify
from .track import Tracker
from .types import StormCell

__all__ = ["run", "identify", "Tracker", "StormCell", "ScitConfig"]


def run(volume: GriddedVolume, config: ScitConfig | None = None) -> list[StormCell]:
    """Identify storm cells in one gridded volume (no tracking).

    One volume in → list of cell objects out, exactly as the spec's data flow
    describes (``grid → detection_v2.run(volume) → cells``).
    """
    return identify(volume, config)
