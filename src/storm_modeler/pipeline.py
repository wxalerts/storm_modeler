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
from .models import GriddedVolume, Warning
from .settings.resolver import DetectionParams, ResolvedSettings, resolve

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
        cells = scit_run(volume, params)
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
