"""Settings resolver — defaults ⊕ overrides → typed params.

``resolve()`` merges registry defaults with PostGIS overrides into a flat dict,
then projects it into typed views: :class:`DetectionParams` for SCIT, plus
convenience accessors for the data window, IEM defaults, and display prefs.

Every :class:`DetectionParams` carries a ``settings_hash`` — a stable digest of
its knob set — so a detection row can be traced back to the exact tuning that
produced it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from pathlib import Path

from .registry import CLOUDTOP_KEYS, DETECTION_KEYS, defaults
from .store import open_store


@dataclass(frozen=True)
class DetectionParams:
    """The SCIT knob set. Passed explicitly to ``detection_v2.run`` — no globals."""

    seed_dbz: float = 40.0
    base_dbz: float = 30.0
    seed_min_separation_km: float = 6.0
    echo_top_min_km: float = 3.0
    continuity_dbz: float = 18.3
    continuity_levels: int = 3
    min_area_km2: float = 4.0
    grid_h_km: float = 1.0
    grid_v_km: float = 0.5
    watershed_split: bool = True
    watershed_min_sep_km: float = 14.0
    track_max_km: float = 12.0
    track_miss_max: int = 2

    @property
    def settings_hash(self) -> str:
        """Stable short digest of the knob set (provenance)."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "DetectionParams":
        return cls(**{k: d[k] for k in DETECTION_KEYS if k in d})


@dataclass(frozen=True)
class CloudTopParams:
    """The GOES ABI cloud-top knob set. Passed explicitly to ``cloudtop.run``.

    Field names drop the ``sat_`` prefix the registry keys carry; ``from_dict``
    maps the resolved ``sat_*`` settings onto them.
    """

    cold_bt_k: float = 235.0
    min_area_km2: float = 25.0
    anvil_bt_k: float = 220.0
    anvil_area_km2: float = 2500.0
    ot_delta_k: float = 6.5
    ot_max_bt_k: float = 205.0
    track_max_km: float = 20.0
    track_miss_max: int = 2
    assoc_max_km: float = 15.0

    @property
    def settings_hash(self) -> str:
        """Stable short digest of the knob set (provenance)."""
        payload = json.dumps(asdict(self), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CloudTopParams":
        # registry key "sat_cold_bt_k" → field "cold_bt_k", etc.
        return cls(**{k[len("sat_"):]: d[k] for k in CLOUDTOP_KEYS if k in d})


@dataclass(frozen=True)
class ResolvedSettings:
    """The full resolved settings dict plus typed projections."""

    values: dict[str, Any]

    @property
    def detection(self) -> DetectionParams:
        return DetectionParams.from_dict(self.values)

    @property
    def pre_minutes(self) -> int:
        return int(self.values["pre_minutes"])

    @property
    def post_minutes(self) -> int:
        return int(self.values["post_minutes"])

    @property
    def iem_default_states(self) -> list[str]:
        raw = str(self.values.get("iem_default_states", "")).strip()
        return [s.strip().upper() for s in raw.split(",") if s.strip()]

    @property
    def iem_default_lookback_hours(self) -> int:
        return int(self.values["iem_default_lookback_hours"])

    # -- Viewer (3D model pane) projections --------------------------------

    @property
    def viewer(self) -> "ViewerParams":
        return ViewerParams.from_values(self.values)

    # -- Satellite (GOES ABI cloud-top) projections ------------------------

    @property
    def cloudtop(self) -> CloudTopParams:
        return CloudTopParams.from_dict(self.values)

    @property
    def sat_bbox_pad_deg(self) -> float:
        return float(self.values.get("sat_bbox_pad_deg", 0.5))

    @property
    def sat_target_res_deg(self) -> float:
        return float(self.values.get("sat_target_res_deg", 0.02))

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


@dataclass(frozen=True)
class ViewerParams:
    """The 3D model-pane knob set (Phase B), projected from resolved settings."""

    vol_floor_dbz: float = 20.0
    iso_levels_dbz: tuple[float, ...] = (40.0, 50.0)
    vert_exag: float = 2.0
    xsection_azimuth_source: str = "track"
    xsection_fixed_bearing: float = 0.0
    grid_cache_size: int = 8

    @staticmethod
    def _parse_levels(raw: Any) -> tuple[float, ...]:
        if isinstance(raw, (list, tuple)):
            items = raw
        else:
            items = str(raw).replace(";", ",").split(",")
        out: list[float] = []
        for tok in items:
            tok = str(tok).strip()
            if not tok:
                continue
            try:
                out.append(float(tok))
            except ValueError:
                continue
        return tuple(sorted(set(out)))

    @classmethod
    def from_values(cls, values: dict[str, Any]) -> "ViewerParams":
        return cls(
            vol_floor_dbz=float(values.get("vol_floor_dbz", 20.0)),
            iso_levels_dbz=cls._parse_levels(values.get("iso_levels_dbz", "40,50")),
            vert_exag=float(values.get("vert_exag", 2.0)),
            xsection_azimuth_source=str(values.get("xsection_azimuth_source", "track")),
            xsection_fixed_bearing=float(values.get("xsection_fixed_bearing", 0.0)),
            grid_cache_size=int(values.get("grid_cache_size", 8)),
        )


def resolve(
    dsn: str | None = None, local_path: str | Path | None = None
) -> ResolvedSettings:
    """Resolve registry defaults ⊕ overrides.

    Overrides come from PostGIS when ``dsn`` is given, else from a local JSON
    file when ``local_path`` is given, else nothing (pure registry defaults, as
    offline tests expect).
    """
    merged = defaults()
    store = open_store(dsn, local_path)
    if store is not None:
        try:
            with store as s:
                merged.update(s.overrides())
        except RuntimeError:
            # No PG_DSN — fall back to pure registry defaults.
            pass
    return ResolvedSettings(values=merged)


def resolve_from_values(values: dict[str, Any]) -> ResolvedSettings:
    merged = defaults()
    merged.update(values)
    return ResolvedSettings(values=merged)
