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
GROUP_VIEWER = "Viewer (3D)"
GROUP_LIGHTNING = "Lightning"
GROUP_SATELLITE = "Satellite (GOES ABI)"
GROUP_HRRR = "Environment (HRRR)"


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
                True, "Split merged multi-core systems into separate cells at "
                "reflectivity saddles (seed split + footprint partition)."),
    SettingSpec("watershed_min_sep_km", "Watershed peak separation (km)",
                GROUP_DETECTION, "float", 14.0, "Minimum distance between split "
                "cores when watershed splitting; larger merges nearby cores "
                "(fewer cells).", min=1.0, max=50.0),
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
    SettingSpec("srm_motion_source", "SRM motion source", GROUP_DISPLAY, "choice",
                "selected_track", "Storm motion framing the SRM map product: "
                "the selected storm's track segment, the mean of all tracks "
                "alive in the volume, or the manual vector below. Display "
                "knob only — detection always runs on base velocity.",
                choices=("selected_track", "mean_tracks", "manual")),
    SettingSpec("srm_manual_speed_kt", "SRM manual speed (kt)", GROUP_DISPLAY,
                "float", 30.0, "Manual storm-motion speed used when the SRM "
                "motion source is 'manual'.", min=0.0, max=120.0),
    SettingSpec("srm_manual_dir_deg", "SRM manual direction (deg)", GROUP_DISPLAY,
                "float", 240.0, "Manual storm-motion direction (meteorological: "
                "the bearing the storm moves FROM; 240 = out of the southwest) "
                "used when the SRM motion source is 'manual'.",
                min=0.0, max=360.0),
    # -- Viewer (3D model pane, Phase B) -----------------------------------
    SettingSpec("vol_floor_dbz", "Volume floor (dBZ)", GROUP_VIEWER, "float",
                20.0, "Reflectivity below this is fully transparent in the 3D "
                "volume render.", min=-30.0, max=80.0),
    SettingSpec("iso_levels_dbz", "Isosurface levels (dBZ)", GROUP_VIEWER, "str",
                "40,50", "Comma-separated dBZ values to draw as isosurface shells."),
    SettingSpec("vert_exag", "Vertical exaggeration", GROUP_VIEWER, "float",
                2.0, "Z-axis stretch applied to the 3D scene so structure is "
                "readable; the axis is labelled in true km.", min=0.1, max=20.0),
    SettingSpec("xsection_azimuth_source", "Cross-section azimuth source",
                GROUP_VIEWER, "choice", "track", "How the vertical cross-section "
                "plane is oriented.", choices=("track", "fixed", "manual")),
    SettingSpec("xsection_fixed_bearing", "Cross-section fixed bearing (deg)",
                GROUP_VIEWER, "float", 0.0, "Section bearing when the azimuth "
                "source is 'fixed' (degrees from north, clockwise).",
                min=0.0, max=360.0),
    SettingSpec("grid_cache_size", "Grid cache size", GROUP_VIEWER, "int",
                8, "Number of re-gridded volumes the viewer keeps in memory "
                "(LRU) for responsive scrubbing.", min=1, max=64),
    # -- Lightning (GOES GLM) ---------------------------------------------
    SettingSpec("lightning_max_flashes", "Max flash markers", GROUP_LIGHTNING, "int",
                5000, "Cap on flash markers placed on the map; above this the "
                "flashes are evenly subsampled.", min=100, max=200000),
    SettingSpec("lightning_bbox_pad_deg", "Bounding-box padding (deg)",
                GROUP_LIGHTNING, "float", 0.5, "Degrees of lat/lon padding around "
                "the warning polygon when keeping flashes.", min=0.0, max=5.0),
    SettingSpec("lightning_quality_good_only", "Good-quality flashes only",
                GROUP_LIGHTNING, "bool", True, "Keep only GLM flashes whose "
                "quality flag is good (flag == 0)."),
    # -- Satellite (GOES ABI Band-13 cloud-top) ---------------------------
    SettingSpec("sat_auto_fetch", "Auto-fetch after download", GROUP_SATELLITE,
                "bool", False, "Automatically run the GOES cloud-top pull for a "
                "warning once its radar download finishes (annotates each volume's "
                "storms with cloud-top temp). Off by default."),
    SettingSpec("sat_cold_bt_k", "Cold-cloud threshold (K)", GROUP_SATELLITE,
                "float", 235.0, "Brightness temperature at/below which a pixel "
                "is cold cloud and seeds a cloud-top object.", min=180.0, max=300.0),
    SettingSpec("sat_min_area_km2", "Min cold-object area (km^2)", GROUP_SATELLITE,
                "float", 25.0, "Minimum cold-object footprint to admit (rejects "
                "speckle).", min=0.0, max=10000.0),
    SettingSpec("sat_anvil_bt_k", "Anvil threshold (K)", GROUP_SATELLITE, "float",
                220.0, "Brightness temperature defining the cold anvil canopy.",
                min=180.0, max=280.0),
    SettingSpec("sat_anvil_area_km2", "Anviled-out area (km^2)", GROUP_SATELLITE,
                "float", 2500.0, "Cold-canopy area at/above which a cell is "
                "flagged 'anviled out'.", min=0.0, max=100000.0),
    SettingSpec("sat_track_max_km", "Cloud-top track max (km)", GROUP_SATELLITE,
                "float", 20.0, "Max displacement between scenes to continue a "
                "cloud-top track.", min=0.0, max=200.0),
    SettingSpec("sat_track_miss_max", "Cloud-top track miss tolerance",
                GROUP_SATELLITE, "int", 2, "Scenes a cloud-top track may be "
                "unmatched before it ends.", min=0, max=10),
    SettingSpec("sat_assoc_max_km", "Radar-association max (km)", GROUP_SATELLITE,
                "float", 15.0, "Max cold-core to radar-core distance to associate "
                "for tilt.", min=0.0, max=100.0),
    SettingSpec("sat_bbox_pad_deg", "Bounding-box padding (deg)", GROUP_SATELLITE,
                "float", 0.5, "Degrees of lat/lon padding around the warning "
                "polygon for the ABI crop.", min=0.0, max=5.0),
    SettingSpec("sat_target_res_deg", "Reproject resolution (deg)", GROUP_SATELLITE,
                "float", 0.02, "Regular lon/lat raster spacing for the "
                "reprojected brightness temperature.", min=0.005, max=0.1),
    # -- Environment (HRRR freezing level + vault detection) ---------------
    SettingSpec("hrrr_vault_dbz", "Vault reflectivity (dBZ)", GROUP_HRRR, "float",
                50.0, "Reflectivity defining the storm's echo tower / vault "
                "core measured against the 0°C level.", min=0.0, max=80.0),
    SettingSpec("hrrr_vault_min_depth_km", "Vault min depth above 0°C (km)",
                GROUP_HRRR, "float", 4.5, "The vault-reflectivity tower must "
                "extend at least this far above the HRRR 0°C level to flag the "
                "cell's overshooting top (~4.5 km is the classic severe-hail "
                "depth).", min=0.0, max=15.0),
    SettingSpec("hrrr_bbox_pad_deg", "Bounding-box padding (deg)", GROUP_HRRR,
                "float", 0.5, "Degrees of lat/lon padding around the radar "
                "volume footprint for the HRRR crop.", min=0.0, max=5.0),
    SettingSpec("hrrr_target_res_deg", "Resample resolution (deg)", GROUP_HRRR,
                "float", 0.03, "Regular lon/lat raster spacing for the "
                "resampled freezing-level grid (native HRRR is ~3 km).",
                min=0.01, max=0.25),
)

REGISTRY_BY_KEY: dict[str, SettingSpec] = {s.key: s for s in REGISTRY}

# Keys whose resolved values constitute the detection knob set (provenance).
DETECTION_KEYS: tuple[str, ...] = (
    "seed_dbz", "base_dbz", "seed_min_separation_km", "echo_top_min_km",
    "continuity_dbz", "continuity_levels", "min_area_km2", "grid_h_km",
    "grid_v_km", "watershed_split", "watershed_min_sep_km", "track_max_km",
    "track_miss_max",
)

# Cloud-top detection knob set (provenance). Crop/reproject keys
# (sat_bbox_pad_deg, sat_target_res_deg) are display/ingest choices, not
# detection knobs, so they are excluded from the hash.
CLOUDTOP_KEYS: tuple[str, ...] = (
    "sat_cold_bt_k", "sat_min_area_km2", "sat_anvil_bt_k", "sat_anvil_area_km2",
    "sat_track_max_km", "sat_track_miss_max", "sat_assoc_max_km",
)

# Vault (HRRR freezing-level) detection knob set (provenance). Crop/resample
# keys are ingest choices, not detection knobs, so they are excluded.
VAULT_KEYS: tuple[str, ...] = (
    "hrrr_vault_dbz", "hrrr_vault_min_depth_km",
)


def get_spec(key: str) -> SettingSpec:
    try:
        return REGISTRY_BY_KEY[key]
    except KeyError:
        raise KeyError(f"unknown setting {key!r}") from None


def defaults() -> dict[str, Any]:
    """Registry defaults as a plain dict (the base layer before overrides)."""
    return {s.key: s.default for s in REGISTRY}
