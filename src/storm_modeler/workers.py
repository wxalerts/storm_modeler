"""Off-thread workers.

All I/O, gridding, SCIT, and persistence run on a ``QThreadPool`` so the GUI
thread never blocks. A :class:`WarningWorker` processes one warning end to end
(its volumes are sequential because tracking is stateful), emitting a Qt signal
per processed volume so the nav tree and map can update incrementally.
"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .config import ScitConfig
from .data.sites import Site
from .data.volumes import VolumeSource
from .models import Warning
from .pipeline import VolumeResult, process_warning


class WorkerSignals(QObject):
    """Signals emitted from a worker thread back to the GUI thread."""

    volume_done = Signal(object)  # VolumeResult
    warning_started = Signal(object)  # Warning
    warning_done = Signal(object)  # Warning
    error = Signal(str)
    finished = Signal()


class WarningWorker(QRunnable):
    """Process a single warning's volumes off the GUI thread."""

    def __init__(
        self,
        warning: Warning,
        volume_source: VolumeSource,
        site: Site,
        config: ScitConfig | None = None,
        dsn: str | None = None,
    ) -> None:
        super().__init__()
        self.warning = warning
        self.volume_source = volume_source
        self.site = site
        self.config = config or ScitConfig()
        self.dsn = dsn
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:  # noqa: D401 - QRunnable entry point
        persistence = None
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
                        self.warning.id, self.warning.event, res.cells
                    )
                self.signals.volume_done.emit(res)

            process_warning(
                self.warning,
                self.volume_source,
                self.site,
                self.config,
                on_result=on_result,
            )
            self.signals.warning_done.emit(self.warning)
        except Exception as e:  # noqa: BLE001
            self.signals.error.emit(f"{e}\n{traceback.format_exc()}")
        finally:
            if persistence is not None:
                persistence.close()
            self.signals.finished.emit()
