"""The data spine, free of any GUI dependency.

Ties the seams together: a warning resolves to a site, the site + bounded
window yields gridded volumes, each volume runs through SCIT, and cells are
tracked across the warning's volumes and (optionally) persisted. The same code
backs both the headless replay path and the GUI workers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

import structlog

from .config import ScitConfig
from .data.sites import Site, SiteResolver
from .data.volumes import FixtureVolumeSource, VolumeSource, bounded_window
from .data.warnings import FixtureWarningSource, WarningSource
from .detection.detection_v2 import StormCell, Tracker, run as scit_run
from .models import GriddedVolume, Warning

log = structlog.get_logger(__name__)


@dataclass
class VolumeResult:
    """One processed volume: the cells SCIT found, tagged to its warning."""

    warning: Warning
    site: Site
    volume: GriddedVolume
    cells: list[StormCell]


@dataclass
class ReplaySummary:
    warnings: int = 0
    volumes: int = 0
    cells: int = 0
    persisted_cells: int = 0
    results: list[VolumeResult] = field(default_factory=list)


# Callback signature: a per-volume result handler (e.g. GUI redraw).
ResultHandler = Callable[[VolumeResult], None]


def process_warning(
    warning: Warning,
    volume_source: VolumeSource,
    site: Site,
    config: ScitConfig | None = None,
    on_result: ResultHandler | None = None,
) -> list[VolumeResult]:
    """Run every volume for one warning through SCIT, tracking across volumes."""
    config = config or ScitConfig()
    tracker = Tracker(config)
    results: list[VolumeResult] = []
    for volume in volume_source:  # oldest -> newest
        cells = scit_run(volume, config)
        tracker.update(cells, volume.valid_time)
        res = VolumeResult(warning=warning, site=site, volume=volume, cells=cells)
        results.append(res)
        log.info(
            "pipeline.volume",
            warning=warning.id,
            valid_time=volume.valid_time.isoformat(),
            cells=len(cells),
        )
        if on_result is not None:
            on_result(res)
    return results


def replay_fixture(
    fixture_dir: str | Path,
    persist: bool = False,
    config: ScitConfig | None = None,
    on_result: ResultHandler | None = None,
    dsn: str | None = None,
) -> ReplaySummary:
    """Deterministic offline replay of a fixture directory (Section 7 A/B).

    Reads ``warning.json`` and the pre-gridded ``volumes/*.npz``, runs the
    pipeline, and optionally upserts to PostGIS.
    """
    fixture_dir = Path(fixture_dir)
    config = config or ScitConfig()
    resolver = SiteResolver()
    summary = ReplaySummary()

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

            vol_source = FixtureVolumeSource(fixture_dir)

            def handle(res: VolumeResult) -> None:
                summary.volumes += 1
                summary.cells += len(res.cells)
                summary.results.append(res)
                if persistence is not None:
                    summary.persisted_cells += persistence.upsert_cells(
                        warning.id, warning.event, res.cells
                    )
                if on_result is not None:
                    on_result(res)

            process_warning(warning, vol_source, site, config, on_result=handle)
    finally:
        if persistence is not None:
            persistence.close()

    log.info(
        "pipeline.replay_done",
        warnings=summary.warnings,
        volumes=summary.volumes,
        cells=summary.cells,
        persisted=summary.persisted_cells,
    )
    return summary
