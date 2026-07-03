"""Off-thread workers.

All I/O, gridding, SCIT, and persistence run on a ``QThreadPool`` so the GUI
thread never blocks. A :class:`WarningWorker` processes one warning end to end
(its volumes are sequential because tracking is stateful), emitting a Qt signal
per processed volume for incremental UI updates and progress. Cancellation is a
checked ``threading.Event``: the worker stops after the in-flight volume, and
every volume already committed remains.
"""

from __future__ import annotations

import threading
import traceback

import structlog
from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .data.sites import Site
from .data.volumes import VolumeSource
from .models import Warning
from .settings.resolver import DetectionParams

log = structlog.get_logger(__name__)


class WorkerSignals(QObject):
    """Signals emitted from a worker thread back to the GUI thread."""

    # (warning, GriddedVolume, index, total) — the worker only downloads + grids;
    # SCIT detection runs on the GUI thread (it creates pyproj transformers, and
    # building those on a QThreadPool worker segfaults in PROJ — see app._on_gridded).
    volume_gridded = Signal(object, object, int, int)
    progress = Signal(int, int, str)  # index, total, label (e.g. "KFWS 1142Z")
    status = Signal(str)  # human-readable sub-step line (download/grid progress)
    warning_started = Signal(object)  # Warning
    warning_done = Signal(object)  # Warning
    error = Signal(str)
    finished = Signal()


class SearchSignals(QObject):
    warning = Signal(object)  # Warning (one per admitted result)
    error = Signal(str)
    finished = Signal()


class LightningSignals(QObject):
    flashes = Signal(object)  # list[Flash] for the warning's window
    status = Signal(str)
    error = Signal(str)
    finished = Signal()


class LightningWorker(QRunnable):
    """Fetch GOES GLM flashes for one warning's window off the GUI thread.

    Lists + downloads + parses the GLM ``LCFA`` files (see
    :class:`storm_modeler.data.lightning.GLMSource`) and emits the flash list
    once, for the map to layer as markers. Cancellation is a checked
    ``threading.Event`` (the global Stop reaches it via ``request_cancel``).
    """

    def __init__(
        self,
        start,
        end,
        bbox,
        bucket: str | None = None,
        max_flashes: int = 5000,
        good_only: bool = True,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.start = start
        self.end = end
        self.bbox = bbox
        self.bucket = bucket
        self.max_flashes = max_flashes
        self.good_only = good_only
        self.cancel = cancel or threading.Event()
        self.signals = LightningSignals()

    def request_cancel(self) -> None:
        self.cancel.set()

    @Slot()
    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        from .data.lightning import GLMSource

        try:
            src = GLMSource(
                self.start, self.end, self.bbox, bucket=self.bucket,
                max_flashes=self.max_flashes, good_only=self.good_only,
            )
            src.set_status_callback(self.signals.status.emit)
            flashes = src.flashes(cancel=self.cancel)
            self.signals.flashes.emit(flashes)
        except Exception as e:  # noqa: BLE001
            log.error("lightning.run_error", error=str(e))
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.signals.finished.emit()


class HRRRSignals(QObject):
    # (warning, FreezingLevelGrid, index, total) — the worker downloads +
    # decodes + resamples (pure NumPy LCC, no pyproj); the GUI thread runs
    # vault detection against the in-memory radar results and refreshes views.
    grid_ready = Signal(object, object, int, int)
    status = Signal(str)  # human-readable sub-step line (idx/download/decode)
    error = Signal(str)
    finished = Signal()


class HRRRWorker(QRunnable):
    """Fetch HRRR 0 °C freezing-level grids for one warning, off the GUI thread.

    Streams hourly analyses from :class:`storm_modeler.data.hrrr.HRRRSource`
    (idx byte-range download + pygrib decode + Lambert-conformal→lon/lat
    resample, all pyproj-free, so worker-safe) and emits each grid for the GUI
    to run vault detection with and cache. Cancellation is a checked
    ``threading.Event``.
    """

    def __init__(
        self,
        warning: Warning,
        grid_source,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.warning = warning
        self.grid_source = grid_source
        self.cancel = cancel or threading.Event()
        self.signals = HRRRSignals()

    def request_cancel(self) -> None:
        self.cancel.set()

    @Slot()
    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        try:
            self.grid_source.set_status_callback(self.signals.status.emit)
            try:
                total = int(self.grid_source.estimated_count())
            except Exception:  # noqa: BLE001
                total = 0
            for i, grid in enumerate(self.grid_source.grids(self.cancel), 1):
                if self.cancel.is_set():
                    break
                total = max(total, i)
                self.signals.grid_ready.emit(self.warning, grid, i, total)
        except Exception as e:  # noqa: BLE001
            log.error("hrrr.run_error", error=str(e))
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.signals.finished.emit()


class SatelliteSignals(QObject):
    # (warning, SatelliteScene, list[CloudTopCell], index, total) — the worker
    # downloads, reprojects (pure NumPy, no pyproj), detects, and tracks; the
    # GUI thread does radar association (it needs the in-memory radar results),
    # the map overlay, and persistence.
    scene_ready = Signal(object, object, object, int, int)
    progress = Signal(int, int, str)  # index, total, label (e.g. "GOES-19 1742Z")
    status = Signal(str)  # human-readable sub-step line (list/download/reproject)
    warning_started = Signal(object)  # Warning
    warning_done = Signal(object)  # Warning
    error = Signal(str)
    finished = Signal()


class SatelliteWorker(QRunnable):
    """Ingest + detect GOES ABI cloud-top cells for one warning, off the GUI thread.

    Streams reprojected scenes from :class:`storm_modeler.data.satellite.ABISource`
    (download + geostationary→lon/lat reprojection are pyproj-free, so they are
    worker-safe), runs cloud-top detection and tracking per scene, and emits each
    scene + its tracked cells for the GUI to associate with radar, render, and
    persist. Cancellation is a checked ``threading.Event``.
    """

    def __init__(
        self,
        warning: Warning,
        scene_source,
        params,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.warning = warning
        self.scene_source = scene_source
        self.params = params
        self.cancel = cancel or threading.Event()
        self.signals = SatelliteSignals()

    def request_cancel(self) -> None:
        self.cancel.set()

    @Slot()
    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        from .detection.cloudtop import Tracker as CloudTopTracker, run as cloudtop_run

        self.signals.warning_started.emit(self.warning)
        try:
            self.scene_source.set_status_callback(self.signals.status.emit)
            try:
                total = int(self.scene_source.estimated_count())
            except Exception:  # noqa: BLE001
                total = 0
            tracker = CloudTopTracker(self.params)
            for i, scene in enumerate(self.scene_source.scenes(self.cancel), 1):
                if self.cancel.is_set():
                    break
                total = max(total, i)
                cells = cloudtop_run(scene, self.params)
                tracker.update(cells, scene.valid_time)
                self.signals.progress.emit(
                    i, total, f"{scene.satellite} {scene.valid_time:%H%MZ}"
                )
                self.signals.scene_ready.emit(self.warning, scene, cells, i, total)
        except Exception as e:  # noqa: BLE001
            log.error("satellite.run_error", error=str(e))
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.signals.warning_done.emit(self.warning)
            self.signals.finished.emit()


class SearchWorker(QRunnable):
    """Run an IEM historical query off the GUI thread, emitting each warning."""

    def __init__(self, start, end, states=None, phenomena=None) -> None:
        super().__init__()
        self.start = start
        self.end = end
        self.states = states
        self.phenomena = phenomena
        self.signals = SearchSignals()

    @Slot()
    def run(self) -> None:
        from .data.warnings import IEMHistoricalSource

        try:
            for w in IEMHistoricalSource(
                self.start, self.end, self.states, phenomena=self.phenomena
            ):
                self.signals.warning.emit(w)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{type(e).__name__}: {e}")
        finally:
            self.signals.finished.emit()


class WarningWorker(QRunnable):
    """Download + grid a warning's volumes off the GUI thread.

    Detection is **not** run here: it builds pyproj transformers, and creating
    those on a ``QThreadPool`` worker segfaults in PROJ on some stacks. The
    worker only does the slow, pyproj-free-for-our-code work (HTTP + Py-ART
    gridding) and hands each raw :class:`GriddedVolume` to the GUI thread, which
    runs SCIT + tracking + persistence (see :meth:`MainWindow._on_gridded`).
    """

    def __init__(
        self,
        warning: Warning,
        volume_source: VolumeSource,
        site: Site,
        params: DetectionParams | None = None,
        dsn: str | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self.warning = warning
        self.volume_source = volume_source
        self.site = site
        self.params = params or DetectionParams()
        self.dsn = dsn
        self.cancel = cancel or threading.Event()
        self.signals = WorkerSignals()

    def request_cancel(self) -> None:
        self.cancel.set()

    @Slot()
    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        log.info("worker.run_begin", warning=self.warning.id, site=self.site.icao,
                 thread=threading.get_ident())
        try:
            self.signals.warning_started.emit(self.warning)
            # Surface the source's sub-step lines (listing/download/grid) to the
            # GUI so the dialog shows motion during each long-running volume.
            self.volume_source.set_status_callback(self.signals.status.emit)
            total = 0
            try:
                total = int(self.volume_source.estimated_count())
            except Exception:  # noqa: BLE001 - estimate is best-effort
                total = 0

            for i, volume in enumerate(self.volume_source, 1):
                if self.cancel.is_set():
                    log.info("worker.cancelled", warning=self.warning.id, after=i - 1)
                    break
                total = max(total, i)
                label = f"{self.site.icao} {volume.valid_time:%H%MZ}"
                self.signals.progress.emit(i, total, label)
                log.info("worker.emit_gridded", warning=self.warning.id, index=i,
                         total=total, valid_time=volume.valid_time.isoformat())
                self.signals.volume_gridded.emit(self.warning, volume, i, total)

            log.info("worker.run_done", warning=self.warning.id)
            self.signals.warning_done.emit(self.warning)
        except Exception as e:  # noqa: BLE001
            log.error("worker.run_error", warning=self.warning.id, error=str(e))
            self.signals.error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            self.signals.finished.emit()
