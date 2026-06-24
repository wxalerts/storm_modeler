"""Settings registry — the single source of every tunable.

No tunable lives as a module constant anymore. Each is a :class:`SettingSpec`
here: detection thresholds, data-window minutes, IEM defaults, and display
preferences. The store overrides defaults in PostGIS; the resolver merges them
into a typed :class:`~storm_modeler.settings.resolver.DetectionParams`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Setting groups (used to organise the settings dialog).
GROUP_DETECTION = "Detection"
GROUP_DATA_WINDOW = "Data window"
GROUP_IEM = "IEM"
GROUP_DISPLAY = "Display"


@dataclass(frozen=True)
class SettingSpec:
    """One tunable: its identity, type, default, and validation bounds."""

    key: str
    label: str
    group: str
    type: str  # "int" | "float" | "bool" | "str" | "choice"
    default: Any
    description: str = ""
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None

    def coerce(self, value: Any) -> Any:
        """Coerce a raw value to this spec's type (raises on bad input)."""
        if self.type == "int":
            return int(value)
        if self.type == "float":
            return float(value)
        if self.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type in ("str", "choice"):
            return str(value)
        raise ValueError(f"unknown setting type {self.type!r}")

    def validate(self, value: Any) -> Any:
        """Coerce and range/choice-check a value, returning the clean value."""
        v = self.coerce(value)
        if self.type in ("int", "float"):
            if self.min is not None and v < self.min:
                raise ValueError(f"{self.key}={v} below min {self.min}")
            if self.max is not None and v > self.max:
                raise ValueError(f"{self.key}={v} above max {self.max}")
        if self.type == "choice" and self.choices and v not in self.choices:
            raise ValueError(f"{self.key}={v!r} not in {self.choices}")
        return v


# --- The registry ----------------------------------------------------------

REGISTRY: tuple[SettingSpec, ...] = (
    # -- Detection --------------------------------------------------------
    SettingSpec("seed_dbz", "Seed reflectivity (dBZ)", GROUP_DETECTION, "float",
                40.0, "Threshold that seeds 3D storm components.", min=0.0, max=80.0),
    SettingSpec("base_dbz", "Base reflectivity (dBZ)", GROUP_DETECTION, "float",
                30.0, "Lower threshold bounding a cell's footprint.", min=0.0, max=80.0),
    SettingSpec("seed_min_separation_km", "Seed min separation (km)", GROUP_DETECTION,
                "float", 6.0, "Minimum spacing between distinct seeds; closer "
                "weaker seeds are suppressed.", min=0.0, max=50.0),
    SettingSpec("echo_top_min_km", "Echo-top minimum (km)", GROUP_DETECTION, "float",
                3.0, "Minimum echo-top height AGL to admit a cell.", min=0.0, max=25.0),
    SettingSpec("continuity_dbz", "Continuity reflectivity (dBZ)", GROUP_DETECTION,
                "float", 18.3, "Threshold defining vertical echo continuity / echo "
                "top.", min=0.0, max=80.0),
    SettingSpec("continuity_levels", "Continuity levels", GROUP_DETECTION, "int",
                3, "Minimum distinct vertical grid levels a cell must span "
                "(rejects AP).", min=1, max=40),
    SettingSpec("min_area_km2", "Minimum footprint (km^2)", GROUP_DETECTION, "float",
                4.0, "Minimum horizontal footprint to reject speckle.", min=0.0,
                max=1000.0),
    SettingSpec("grid_h_km", "Grid horizontal spacing (km)", GROUP_DETECTION, "float",
                1.0, "Cartesian analysis grid horizontal spacing.", min=0.25, max=5.0),
    SettingSpec("grid_v_km", "Grid vertical spacing (km)", GROUP_DETECTION, "float",
                0.5, "Cartesian analysis grid vertical spacing.", min=0.1, max=2.0),
    SettingSpec("watershed_split", "Watershed cell splitting", GROUP_DETECTION, "bool",
                False, "Split merged reflectivity blobs via watershed."),
    SettingSpec("track_max_km", "Track max displacement (km)", GROUP_DETECTION, "float",
                12.0, "Max centroid displacement between volumes to associate a "
                "track.", min=0.0, max=100.0),
    SettingSpec("track_miss_max", "Track miss tolerance", GROUP_DETECTION, "int",
                2, "Volumes a track may be unmatched before it ends.", min=0, max=10),
    # -- Data window ------------------------------------------------------
    SettingSpec("pre_minutes", "Pre-window (min)", GROUP_DATA_WINDOW, "int",
                60, "Minutes before issuance to start the volume pull.",
                min=0, max=240),
    SettingSpec("post_minutes", "Post-window (min)", GROUP_DATA_WINDOW, "int",
                30, "Minutes after expiry to end the volume pull.", min=0, max=240),
    # -- IEM defaults -----------------------------------------------------
    SettingSpec("iem_default_states", "Default states/WFO", GROUP_IEM, "str",
                "", "Comma-separated default states for the IEM search."),
    SettingSpec("iem_default_lookback_hours", "Default lookback (hours)", GROUP_IEM,
                "int", 12, "Default search window length pre-filled in the form.",
                min=1, max=720),
    # -- Display ----------------------------------------------------------
    SettingSpec("dbz_color_table", "dBZ color table", GROUP_DISPLAY, "choice",
                "NWS", "Reflectivity color table for the map.", choices=("NWS",)),
    SettingSpec("show_counties", "Show counties", GROUP_DISPLAY, "bool", True,
                "Toggle the counties basemap layer."),
    SettingSpec("show_states", "Show states", GROUP_DISPLAY, "bool", True,
                "Toggle the states basemap layer."),
    SettingSpec("show_highways", "Show highways", GROUP_DISPLAY, "bool", True,
                "Toggle the highways basemap layer."),
)

REGISTRY_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in REGISTRY}

# Keys whose resolved values constitute the detection knob set (provenance).
DETECTION_KEYS: tuple[str, ...] = (
    "seed_dbz", "base_dbz", "seed_min_separation_km", "echo_top_min_km",
    "continuity_dbz", "continuity_levels", "min_area_km2", "grid_h_km",
    "grid_v_km", "watershed_split", "track_max_km", "track_miss_max",
)


def get_spec(key: str) -> SettingSpec:
    try:
        return REGISTRY_BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown setting {key!r}") from None


def defaults() -> dict[str, Any]:
    """Registry defaults as a plain dict (the base layer before overrides)."""
    return {s.key: s.default for s in REGISTRY}
