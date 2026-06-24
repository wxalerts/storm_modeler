"""Central configuration for storm_modeler.

Values are deliberately conservative and deterministic. Nothing here reads a
live clock; every tunable that affects detection lives in :class:`ScitConfig`
so reruns are reproducible.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# --- Filesystem layout -----------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_ROOT.parent.parent
DATA_DIR = REPO_ROOT / "data"
SHAPEFILE_DIR = DATA_DIR / "shapefiles"
FIXTURE_DIR = PACKAGE_ROOT / "tests" / "fixtures"

# On-disk cache for pulled IEM warning sets, keyed by date range, so reruns
# are offline (Section 1a).
CACHE_DIR = Path(os.environ.get("STORM_MODELER_CACHE", REPO_ROOT / ".cache"))


# --- Database --------------------------------------------------------------

def pg_dsn() -> str | None:
    """Return the PostGIS DSN from the environment (``PG_DSN``), or ``None``.

    Section 7 sets ``PG_DSN`` to the local PostGIS. When unset, persistence is
    skipped (the pipeline still runs and emits to the GUI / stdout).
    """
    return os.environ.get("PG_DSN") or None


# --- Bounded archive window (Section 2) ------------------------------------

#: Volumes are pulled for ``issued - PRE_MINUTES .. expires + POST_MINUTES``.
PRE_MINUTES = 60
POST_MINUTES = 30


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


@dataclass(frozen=True)
class ScitConfig:
    """Tunables for the SCIT identification + tracking pass.

    The defaults encode the Phase-1 guarantee that anomalous propagation (AP)
    never seeds: a candidate must show vertical continuity (presence across
    several grid levels, a minimum depth, and a minimum echo top) before it is
    admitted as a storm cell. Ground-only clutter fails all three.
    """

    #: Reflectivity threshold (dBZ) used to seed 3D storm components.
    seed_dbz: float = 40.0
    #: Reflectivity threshold (dBZ) defining the echo top.
    echo_top_dbz: float = 18.3
    #: Minimum number of distinct vertical grid levels a component must span.
    min_levels: int = 3
    #: Minimum vertical depth (km) of the seeded region.
    min_depth_km: float = 3.0
    #: Minimum echo-top height (km AGL).
    min_echo_top_km: float = 3.0
    #: Minimum horizontal footprint (km^2) to reject speckle.
    min_area_km2: float = 4.0
    #: Max storm motion (m/s) used to gate frame-to-frame track association.
    max_track_speed_ms: float = 40.0

    @classmethod
    def from_env(cls) -> "ScitConfig":
        def _f(name: str, default: float) -> float:
            v = os.environ.get(name)
            return float(v) if v is not None else default

        return cls(
            seed_dbz=_f("SCIT_SEED_DBZ", cls.seed_dbz),
            echo_top_dbz=_f("SCIT_ECHO_TOP_DBZ", cls.echo_top_dbz),
            min_levels=int(_f("SCIT_MIN_LEVELS", cls.min_levels)),
            min_depth_km=_f("SCIT_MIN_DEPTH_KM", cls.min_depth_km),
            min_echo_top_km=_f("SCIT_MIN_ECHO_TOP_KM", cls.min_echo_top_km),
            min_area_km2=_f("SCIT_MIN_AREA_KM2", cls.min_area_km2),
            max_track_speed_ms=_f("SCIT_MAX_TRACK_SPEED_MS", cls.max_track_speed_ms),
        )


@dataclass(frozen=True)
class GridConfig:
    """Cartesian analysis grid for gridding archived volumes.

    A local azimuthal-equidistant tangent plane centred on the radar. Heights
    are AGL. These bounds comfortably contain a single-site storm complex.
    """

    half_width_km: float = 150.0
    horizontal_spacing_km: float = 1.0
    z_min_km: float = 0.5
    z_max_km: float = 18.0
    z_spacing_km: float = 0.5

    def nx(self) -> int:
        return int(round(2 * self.half_width_km / self.horizontal_spacing_km)) + 1

    def nz(self) -> int:
        return int(round((self.z_max_km - self.z_min_km) / self.z_spacing_km)) + 1


DEFAULT_SCIT = ScitConfig()
DEFAULT_GRID = GridConfig()
