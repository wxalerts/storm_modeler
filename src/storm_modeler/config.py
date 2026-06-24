"""Static configuration: filesystem layout, DSN, the source filter, grid extent.

Tunables do **not** live here — they live in
:mod:`storm_modeler.settings.registry` and are resolved at runtime. This module
holds only fixed facts (paths, the VTEC source filter, the analysis-grid extent)
that are not knobs a user tunes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# --- Filesystem layout -----------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
DATA_DIR = REPO_ROOT / "data"
SHAPEFILE_DIR = DATA_DIR / "shapefiles"
FIXTURE_DIR = PACKAGE_ROOT / "tests" / "fixtures"

#: On-disk cache for pulled IEM warning sets, keyed by query, so reruns are
#: offline (Section 1a).
CACHE_DIR = Path(os.environ.get("STORM_MODELER_CACHE", REPO_ROOT / ".cache"))


# --- Database --------------------------------------------------------------

def pg_dsn() -> str | None:
    """Return the PostGIS DSN from the environment (``PG_DSN``), or ``None``."""
    return os.environ.get("PG_DSN") or None


# --- Source filter (Section 1a) --------------------------------------------

#: VTEC phenomena admitted by the historical source. Everything else dropped.
ADMITTED_PHENOMENA = frozenset({"TO", "SV"})
#: VTEC significance admitted (Warning only).
ADMITTED_SIGNIFICANCE = "W"

EVENT_NAMES = {
    ("TO", "W"): "Tornado Warning",
    ("SV", "W"): "Severe Thunderstorm Warning",
}


def event_name(phenomena: str, significance: str) -> str:
    return EVENT_NAMES.get((phenomena, significance), f"{phenomena}.{significance}")


# --- Analysis grid extent --------------------------------------------------

@dataclass(frozen=True)
class GridConfig:
    """Cartesian analysis-grid *extent* for gridding archived volumes.

    A local azimuthal-equidistant tangent plane centred on the radar; heights
    are AGL. The horizontal/vertical *spacing* are tunables (``grid_h_km`` /
    ``grid_v_km`` in the registry) and are supplied per run.
    """

    half_width_km: float = 150.0
    z_min_km: float = 0.5
    z_max_km: float = 18.0

    def nx(self, h_km: float) -> int:
        return int(round(2 * self.half_width_km / h_km)) + 1

    def nz(self, v_km: float) -> int:
        return int(round((self.z_max_km - self.z_min_km) / v_km)) + 1


DEFAULT_GRID = GridConfig()
