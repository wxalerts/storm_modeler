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
from .pipeline import VolumeResult, process_warning
from .settings.resolver import DetectionParams

log = structlog.get_logger(__name__)


class WorkerSignals(QObject):
    """Signals emitted from a worker thread back to the GUI thread."""

    volume_done = Signal(object)  # VolumeResult
    progress = Signal(int, int, str)  # index, total, label (e.g. "KFWS 1142Z")
    warning_started = Signal(object)  # Warning
    warning_done = Signal(object)  # Warning
    error = Signal(str)
    finished = Signal()


class SearchSignals(QObject):
    warning = Signal(object)  # Warning (one per admitted result)
    error = Signal(str)
    finished = Signal()


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
    """Process a single warning's volumes off the GUI thread."""

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
        persistence = None
        log.info("worker.run_begin", warning=self.warning.id, site=self.site.icao,
                 thread=threading.get_ident())
        try:
            if self.dsn:
                from .persist import Persistence

                persistence = Persistence(self.dsn)
                persistence.connect()
                persistence.upsert_warning(self.warning)

            self.signals.warning_started.emit(self.warning)

            def on_result(res: VolumeResult) -> None:
                if persistence is not None:
                    persistence.upsert_cells(
                        self.warning.id, self.warning.event, res.cells,
                        self.params.settings_hash,
                    )
                log.info("worker.emit_volume", warning=self.warning.id,
                         index=res.index, total=res.total, cells=len(res.cells))
                self.signals.volume_done.emit(res)

            def on_progress(i: int, total: int, volume) -> None:
                label = f"{self.site.icao} {volume.valid_time:%H%MZ}"
                self.signals.progress.emit(i, total, label)

            process_warning(
                self.warning, self.volume_source, self.site, self.params,
                on_result=on_result, on_progress=on_progress, cancel=self.cancel,
            )
            log.info("worker.run_done", warning=self.warning.id)
            self.signals.warning_done.emit(self.warning)
        except Exception as e:  # noqa: BLE001
            log.error("worker.run_error", warning=self.warning.id, error=str(e))
            self.signals.error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            if persistence is not None:
                persistence.close()
            self.signals.finished.emit()
