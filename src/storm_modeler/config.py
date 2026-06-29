"""Static configuration: filesystem layout, DSN, the source filter, grid extent.

Tunables do **not** live here — they live in
:mod:`storm_modeler.settings.registry` and are resolved at runtime. This module
holds only fixed facts (paths, the VTEC source filter, the analysis-grid extent)
that are not knobs a user tunes.
"""

from __future__ import annotations

import importlib.util
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


# --- Live-extra pre-checks -------------------------------------------------

#: Modules the IEM search path imports lazily (see ``data/warnings.py``).
#: ``shapely``/``pyproj`` are core deps; ``httpx`` and ``pyogrio`` ship only in
#: the ``live`` extra, so these are the ones that can actually be absent.
IEM_SEARCH_MODULES = ("httpx", "pyogrio")

#: Modules the GOES GLM lightning pull imports lazily (see ``data/lightning.py``).
#: ``boto3`` is the live S3 client; ``netCDF4`` reads the LCFA files (it rides
#: along with ``arm-pyart``).
LIGHTNING_MODULES = ("boto3", "netCDF4")

#: Modules the GOES ABI cloud-top pull imports lazily (see ``data/satellite.py``).
#: ``boto3`` lists/downloads from S3; ``xarray`` opens the CMIP NetCDF; ``goes2go``
#: provides geolocation helpers. All ship in the ``live`` extra.
SATELLITE_MODULES = ("boto3", "xarray", "goes2go")

#: Hint shown when a live module is missing.
LIVE_EXTRA_HINT = "run: uv sync --extra live"


def missing_modules(modules: tuple[str, ...]) -> list[str]:
    """Return the subset of *modules* that can't be imported.

    Uses :func:`importlib.util.find_spec` so it never actually imports the
    module — cheap, and it avoids touching PROJ on a worker thread.
    """
    return [m for m in modules if importlib.util.find_spec(m) is None]


# --- Database --------------------------------------------------------------

def pg_dsn() -> str | None:
    """Return the PostGIS DSN from the environment (``PG_DSN``), or ``None``."""
    return os.environ.get("PG_DSN") or None


def local_settings_path() -> Path:
    """On-disk settings file used to persist overrides when no ``PG_DSN`` is set.

    A JSON fallback for the PostGIS ``app_settings`` table, so the desktop app
    keeps tuning across restarts without a database. Overridable via
    ``STORM_MODELER_SETTINGS``.
    """
    env = os.environ.get("STORM_MODELER_SETTINGS")
    return Path(env) if env else CACHE_DIR / "settings.json"


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
