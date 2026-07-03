"""The data spine, free of any GUI dependency.

Resolves the active settings (defaults ⊕ DB overrides), then ties the seams
together: a warning resolves to a site, the site + bounded window yields gridded
volumes, each volume runs through SCIT with the resolved :class:`DetectionParams`,
and cells are tracked across the warning's volumes and (optionally) persisted —
**committed per volume**, stamped with the ``settings_hash`` for provenance, so a
cancel keeps everything already done. The same code backs the headless replay
path and the GUI workers.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import structlog

from .data.sites import Site, SiteResolver
from .data.volumes import FixtureVolumeSource, VolumeSource
from .data.warnings import FixtureWarningSource
from .detection.detection_v2 import StormCell, Tracker, run as scit_run
from .detection.cloudtop import (
    CloudTopCell,
    Tracker as CloudTopTracker,
    associate_radar,
    run as cloudtop_run,
)
from .detection.vault import detect_vault
from .models import FreezingLevelGrid, GriddedVolume, SatelliteScene, Warning
from .settings.resolver import (
    CloudTopParams,
    DetectionParams,
    ResolvedSettings,
    VaultParams,
    resolve,
)

log = structlog.get_logger(__name__)


@dataclass
class VolumeResult:
    """One processed volume: the cells SCIT found, tagged to its warning."""

    warning: Warning
    site: Site
    volume: GriddedVolume
    cells: list[StormCell]
    index: int = 0
    total: int = 0
    settings_hash: str = ""
    # Rotation couplets detected in the volume's velocity layer (addon
    # annotation, filled by the app after SCIT; empty when not computed).
    couplets: list = field(default_factory=list)


@dataclass
class ReplaySummary:
    warnings: int = 0
    volumes: int = 0
    cells: int = 0
    persisted_cells: int = 0
    settings_hash: str = ""
    results: list[VolumeResult] = field(default_factory=list)


# Callback signatures.
ResultHandler = Callable[[VolumeResult], None]
ProgressHandler = Callable[[int, int, GriddedVolume], None]


def process_warning(
    warning: Warning,
    volume_source: VolumeSource,
    site: Site,
    params: DetectionParams | None = None,
    on_result: ResultHandler | None = None,
    on_progress: ProgressHandler | None = None,
    cancel: threading.Event | None = None,
) -> list[VolumeResult]:
    """Run every volume for one warning through SCIT, tracking across volumes.

    Honours ``cancel``: the loop stops before fetching/processing the next
    volume once the event is set, so all volumes already handed to ``on_result``
    (and thus committed) remain. No rollback.
    """
    params = params or DetectionParams()
    tracker = Tracker(params)
    results: list[VolumeResult] = []

    # Stream volumes one at a time rather than materialising the whole window:
    # progress advances as each grids, results (and their renders) arrive
    # incrementally instead of in one burst, and a cancel takes effect between
    # volumes. ``total`` is fetched cheaply up front when the source can (e.g.
    # THREDDS lists its catalog without downloading), else it is left open.
    total = 0
    try:
        total = int(volume_source.estimated_count())
    except Exception:  # noqa: BLE001 - estimate is best-effort
        total = 0

    for i, volume in enumerate(volume_source, 1):
        if cancel is not None and cancel.is_set():
            log.info("pipeline.cancelled", warning=warning.id, after=i - 1)
            break
        total = max(total, i)
        log.info("pipeline.detect_begin", warning=warning.id, index=i, total=total,
                 valid_time=volume.valid_time.isoformat(), shape=volume.shape)
        cells = scit_run(volume, params)
        log.info("pipeline.detect_done", warning=warning.id, index=i,
                 cells=len(cells))
        tracker.update(cells, volume.valid_time)
        res = VolumeResult(
            warning=warning, site=site, volume=volume, cells=cells,
            index=i, total=total, settings_hash=params.settings_hash,
        )
        results.append(res)
        log.info(
            "pipeline.volume", warning=warning.id,
            valid_time=volume.valid_time.isoformat(),
            cells=len(cells), index=i, total=total,
        )
        if on_progress is not None:
            on_progress(i, total, volume)
        if on_result is not None:
            on_result(res)
    return results


@dataclass
class SatelliteResult:
    """One processed ABI scene: the cloud-top cells found, tagged to its warning."""

    warning: Warning
    scene: SatelliteScene
    cloudtops: list[CloudTopCell]
    index: int = 0
    total: int = 0
    settings_hash: str = ""


SatelliteResultHandler = Callable[[SatelliteResult], None]
SatelliteProgressHandler = Callable[[int, int, SatelliteScene], None]


def _nearest_radar_cells(
    radar_results: list[VolumeResult], when: datetime
) -> list[StormCell]:
    """Cells of the radar volume whose valid_time is closest to ``when``."""
    if not radar_results:
        return []
    nearest = min(
        radar_results,
        key=lambda r: abs((r.volume.valid_time - when).total_seconds()),
    )
    return nearest.cells


def process_warning_satellite(
    warning: Warning,
    scene_source,
    params: CloudTopParams | None = None,
    radar_results: list[VolumeResult] | None = None,
    on_result: SatelliteResultHandler | None = None,
    on_progress: SatelliteProgressHandler | None = None,
    cancel: threading.Event | None = None,
) -> list[SatelliteResult]:
    """Run every ABI scene for one warning through cloud-top detection.

    Streams scenes oldest→newest: each is identified, tracked across scenes, and
    — when ``radar_results`` is given — associated to the nearest-in-time radar
    volume to measure storm tilt. Mirrors :func:`process_warning`; honours
    ``cancel`` between scenes, keeping everything already handed to ``on_result``.
    """
    params = params or CloudTopParams()
    tracker = CloudTopTracker(params)
    results: list[SatelliteResult] = []

    total = 0
    try:
        total = int(scene_source.estimated_count())
    except Exception:  # noqa: BLE001 - estimate is best-effort
        total = 0

    for i, scene in enumerate(scene_source, 1):
        if cancel is not None and cancel.is_set():
            log.info("pipeline.sat_cancelled", warning=warning.id, after=i - 1)
            break
        total = max(total, i)
        cells = cloudtop_run(scene, params)
        tracker.update(cells, scene.valid_time)
        if radar_results:
            associate_radar(
                cells, _nearest_radar_cells(radar_results, scene.valid_time), params
            )
        res = SatelliteResult(
            warning=warning, scene=scene, cloudtops=cells,
            index=i, total=total, settings_hash=params.settings_hash,
        )
        results.append(res)
        log.info(
            "pipeline.scene", warning=warning.id,
            valid_time=scene.valid_time.isoformat(),
            cloudtops=len(cells), index=i, total=total,
        )
        if on_progress is not None:
            on_progress(i, total, scene)
        if on_result is not None:
            on_result(res)
    return results


def annotate_vault_results(
    results: list[VolumeResult],
    grids: list[FreezingLevelGrid],
    params: VaultParams | None = None,
) -> list[VolumeResult]:
    """Annotate every volume's cells with vault metrics vs the 0 °C level.

    Each volume is paired with the HRRR freezing-level grid nearest in time
    (the 0 °C surface moves slowly, so the hourly analyses bracket every radar
    volume) and its cells are annotated in place by
    :func:`~storm_modeler.detection.vault.detect_vault` — including the derived
    ``overshooting_top`` flag. Pure given its inputs; a no-op without grids.
    """
    if not grids:
        return results
    params = params or VaultParams()
    for res in results:
        grid = min(
            grids,
            key=lambda g: abs((g.valid_time - res.volume.valid_time).total_seconds()),
        )
        detect_vault(res.volume, res.cells, grid, res.site.elevation_m, params)
    return results


def replay_fixture(
    fixture_dir: str | Path,
    persist: bool = False,
    settings: ResolvedSettings | None = None,
    on_result: ResultHandler | None = None,
    dsn: str | None = None,
    cancel: threading.Event | None = None,
) -> ReplaySummary:
    """Deterministic offline replay of a fixture directory (Section 8A).

    Resolves settings from the store (unless provided), runs the pipeline with
    the resolved :class:`DetectionParams`, and optionally upserts to PostGIS with
    the active ``settings_hash``.
    """
    fixture_dir = Path(fixture_dir)
    settings = settings or resolve(dsn)
    params = settings.detection
    resolver = SiteResolver()
    summary = ReplaySummary(settings_hash=params.settings_hash)

    persistence = None
    if persist:
        from .persist import Persistence

        persistence = Persistence(dsn)
        persistence.connect()

    try:
        for warning in FixtureWarningSource(fixture_dir):
            summary.warnings += 1
            site = resolver.for_polygon(warning.polygon)
            if persistence is not None:
                persistence.upsert_warning(warning)

            def handle(res: VolumeResult) -> None:
                summary.volumes += 1
                summary.cells += len(res.cells)
                summary.results.append(res)
                if persistence is not None:
                    # Per-volume commit, stamped with provenance.
                    summary.persisted_cells += persistence.upsert_cells(
                        warning.id, warning.event, res.cells, params.settings_hash
                    )
                if on_result is not None:
                    on_result(res)

            process_warning(
                warning, FixtureVolumeSource(fixture_dir), site, params,
                on_result=handle, cancel=cancel,
            )
    finally:
        if persistence is not None:
            persistence.close()

    log.info(
        "pipeline.replay_done", warnings=summary.warnings, volumes=summary.volumes,
        cells=summary.cells, persisted=summary.persisted_cells,
        settings_hash=summary.settings_hash,
    )
    return summary
