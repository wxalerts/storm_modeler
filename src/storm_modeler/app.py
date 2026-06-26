"""QApplication + QMainWindow + 3-pane splitter, and the CLI entry point.

Modes (Section 7):

* ``--headless --replay <dir> [--persist]`` — run the deterministic fixture
  replay with no GUI and exit. This is the validation A/B path.
* ``--smoke`` — build the QApplication and all three panes offscreen, run one
  fixture through them to prove the wiring, and exit 0. Validation C.
* (default) — launch the interactive GUI. ``--replay <dir>`` preloads a fixture;
  ``--from/--to`` drives a live IEM historical pull.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .config import FIXTURE_DIR, pg_dsn

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Headless replay (no Qt)
# ---------------------------------------------------------------------------

def run_headless(args: argparse.Namespace) -> int:
    from .pipeline import replay_fixture

    if not args.replay:
        print("--headless requires --replay <fixture_dir>", file=sys.stderr)
        return 2

    dsn = pg_dsn() if args.persist else None
    if args.persist and not dsn:
        print("--persist set but PG_DSN is not configured", file=sys.stderr)
        return 2

    # Settings (incl. detection knobs) are resolved from the store inside
    # replay_fixture, so a `set_setting` write is reflected here with no code edit.
    summary = replay_fixture(Path(args.replay), persist=args.persist, dsn=dsn)
    print(
        f"replay: warnings={summary.warnings} volumes={summary.volumes} "
        f"cells={summary.cells} persisted={summary.persisted_cells}"
    )
    return 0


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _fixture_dirs():
    """Shipped fixture directories that contain a warning.json."""
    return sorted(p for p in FIXTURE_DIR.iterdir() if (p / "warning.json").exists())


def _build_window(persist: bool):
    import threading

    from PySide6.QtCore import Qt, QThreadPool
    from PySide6.QtWidgets import QMainWindow, QSplitter, QStatusBar

    from .data.sites import SiteResolver
    from .data.volumes import FixtureVolumeSource, ThreddsLevel2Source, bounded_window
    from .data.warnings import FixtureWarningSource
    from .dialogs.download_dialog import DownloadDialog
    from .dialogs.settings_dialog import SettingsDialog
    from .panes.left_search import LeftSearchPane
    from .panes.left_volumes import LeftVolumesPane
    from .panes.map_view import MapPane
    from .panes.model_view import ModelPane
    from .settings.resolver import resolve
    from .workers import SearchWorker, WarningWorker

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("WxAlerts Storm Modeler — Phase A")
            self.resize(1600, 950)

            self.dsn = pg_dsn() if persist else None
            self.settings = resolve(self.dsn)
            self.params = self.settings.detection
            self.resolver = SiteResolver()
            self.pool = QThreadPool.globalInstance()
            self._workers: list = []
            self._sources: dict[str, object] = {}   # warning.id -> () -> VolumeSource
            self._results: dict[str, list] = {}      # warning.id -> [VolumeResult]
            self._selected_id: str | None = None

            # Panes.
            self.search = LeftSearchPane(self.settings)
            self.volumes = LeftVolumesPane()
            self.map = MapPane()
            self.model = ModelPane(self.settings)

            # Left panel: vertical split (search on top, volumes below).
            left = QSplitter(Qt.Vertical)
            left.addWidget(self.search)
            left.addWidget(self.volumes)
            left.setSizes([520, 420])

            outer = QSplitter(Qt.Horizontal)
            outer.addWidget(left)
            outer.addWidget(self.map)
            outer.addWidget(self.model)
            outer.setStretchFactor(0, 0)
            outer.setStretchFactor(1, 1)
            outer.setStretchFactor(2, 0)
            outer.setSizes([260, 960, 360])  # left ~15%, draggable
            self.setCentralWidget(outer)

            self.setStatusBar(QStatusBar())
            self.map.set_basemap()
            self._build_menu()

            # Record the GL context once the render windows exist — the first
            # thing to inspect if a render crashes on this display stack.
            from .logging_setup import log_gl_info
            log_gl_info(self.map.plotter, where="map")
            log_gl_info(self.model.plotter, where="model3d")

            # Wiring.
            self.search.search_requested.connect(self.on_search)
            self.search.warning_selected.connect(self.on_select)
            self.search.download_requested.connect(self.on_download)
            # _on_storm drives both the map (radar/cells for the cell's volume)
            # and the 3D pane; it falls back to a plain highlight if the volume
            # isn't found.
            self.volumes.storm_selected.connect(self._on_storm)

        # --- menu ---------------------------------------------------------

        def _build_menu(self) -> None:
            run_menu = self.menuBar().addMenu("&Run")
            run_menu.addAction("Load shipped fixtures").triggered.connect(self.load_fixtures)
            run_menu.addSeparator()
            run_menu.addAction("Stop").triggered.connect(self.stop)
            settings_menu = self.menuBar().addMenu("&Settings")
            settings_menu.addAction("Open settings…").triggered.connect(self.open_settings)

        def open_settings(self) -> None:
            dlg = SettingsDialog(self.dsn, self)
            dlg.settingsChanged.connect(self.reload_settings)
            dlg.exec()

        def reload_settings(self) -> None:
            self.settings = resolve(self.dsn)
            self.params = self.settings.detection
            self.model.set_settings(self.settings)
            self.statusBar().showMessage(
                f"Settings reloaded (detection hash {self.params.settings_hash})"
            )

        # --- search / results --------------------------------------------

        def on_search(self, params) -> None:
            self.search.clear_results()
            phenomena = ([] + (["TO"] if params.tornado else [])
                         + (["SV"] if params.severe else []))
            if not phenomena:
                self.statusBar().showMessage("Select at least one event type.")
                return
            self.search.set_searching(True)
            worker = SearchWorker(
                params.start, params.end, params.states or None, phenomena=phenomena
            )
            worker.signals.warning.connect(lambda w: self._add_result(w, params))
            worker.signals.error.connect(
                lambda m: self.statusBar().showMessage(f"IEM search failed: {m}")
            )
            worker.signals.finished.connect(lambda: self.search.set_searching(False))
            self._workers.append(worker)
            self.pool.start(worker)

        def _add_result(self, warning, params) -> None:
            site = self.resolver.for_polygon(warning.polygon)
            w0, w1 = bounded_window(
                warning.issued, warning.expires, params.pre_minutes, params.post_minutes
            )
            self._sources[warning.id] = lambda: ThreddsLevel2Source(
                site.icao, w0, w1, site.lat, site.lon,
                h_km=self.params.grid_h_km, v_km=self.params.grid_v_km,
            )
            self.search.add_result(warning)

        def load_fixtures(self) -> None:
            """Offline: populate results from the shipped replay fixtures."""
            self.search.clear_results()
            for d in _fixture_dirs():
                for warning in FixtureWarningSource(d):
                    self._sources[warning.id] = (lambda dd: (lambda: FixtureVolumeSource(dd)))(d)
                    self.search.add_result(warning)
            self.statusBar().showMessage("Loaded shipped fixtures.")

        def on_select(self, warning) -> None:
            self._selected_id = warning.id
            self.volumes.set_warning(warning)
            self.map.show_warning(warning)
            for res in self._results.get(warning.id, []):
                self.volumes.add_result(res)
            self.statusBar().showMessage(f"Selected {warning.event} ETN {warning.etn:04d}")

        # --- download / processing ---------------------------------------

        def on_download(self, warning) -> None:
            factory = self._sources.get(warning.id)
            if factory is None:
                self.statusBar().showMessage("No volume source for that warning.")
                return
            site = self.resolver.for_polygon(warning.polygon)
            self._selected_id = warning.id
            self._results[warning.id] = []
            self.volumes.set_warning(warning)
            self.map.show_warning(warning)

            log.info("download.start", warning=warning.id, event_name=warning.event,
                     site=site.icao, issued=warning.issued.isoformat(),
                     expires=warning.expires.isoformat())
            # Ensure the Py-ART/cartopy/pyproj stack is imported on THIS (main)
            # thread before the worker grids — importing it on the worker
            # segfaults in pyproj's non-thread-safe CRS init.
            warmup_grid_stack()

            # Detection + tracking + persistence run on the GUI thread (pyproj
            # is unsafe to use on the QThreadPool worker); the worker only grids.
            from .detection.detection_v2 import Tracker
            self._dl_tracker = Tracker(self.params)
            self._dl_persistence = None
            if self.dsn:
                from .persist import Persistence
                self._dl_persistence = Persistence(self.dsn)
                self._dl_persistence.connect()
                self._dl_persistence.upsert_warning(warning)

            cancel = threading.Event()
            dialog = DownloadDialog(f"{site.icao}  {warning.event}", parent=self)
            worker = WarningWorker(warning, factory(), site, self.params, self.dsn, cancel)
            worker.signals.progress.connect(dialog.update_progress)
            worker.signals.volume_gridded.connect(self._on_gridded)
            worker.signals.error.connect(self._on_error)
            worker.signals.finished.connect(dialog.finish)
            worker.signals.finished.connect(self._close_download_persistence)
            dialog.cancel_requested.connect(worker.request_cancel)
            self._workers.append(worker)
            self.pool.start(worker)
            dialog.show()

        def _close_download_persistence(self) -> None:
            if getattr(self, "_dl_persistence", None) is not None:
                self._dl_persistence.close()
                self._dl_persistence = None

        def stop(self) -> None:
            for w in self._workers:
                if hasattr(w, "request_cancel"):
                    w.request_cancel()
            self.pool.clear()
            self.statusBar().showMessage("Stopped (cancelled in-flight; queue cleared).")

        # --- signals ------------------------------------------------------

        def _on_gridded(self, warning, volume, index, total) -> None:
            """GUI-thread SCIT detection + tracking on a freshly gridded volume.

            Runs here (not on the worker) because SCIT builds pyproj transformers
            and doing that on the QThreadPool worker segfaults in PROJ.
            """
            from .detection.detection_v2 import run as scit_run
            from .pipeline import VolumeResult

            log.info("gui.detect_begin", warning=warning.id, index=index, total=total,
                     valid_time=volume.valid_time.isoformat())
            try:
                cells = scit_run(volume, self.params)
                self._dl_tracker.update(cells, volume.valid_time)
            except Exception as e:  # noqa: BLE001
                log.error("gui.detect_error", warning=warning.id, index=index,
                          error=str(e))
                return
            log.info("gui.detect_done", warning=warning.id, index=index,
                     cells=len(cells))

            site = self.resolver.for_polygon(warning.polygon)
            res = VolumeResult(
                warning=warning, site=site, volume=volume, cells=cells,
                index=index, total=total, settings_hash=self.params.settings_hash,
            )
            if getattr(self, "_dl_persistence", None) is not None:
                try:
                    self._dl_persistence.upsert_cells(
                        warning.id, warning.event, cells, self.params.settings_hash
                    )
                except Exception as e:  # noqa: BLE001
                    log.error("gui.persist_error", warning=warning.id, error=str(e))
            self._on_volume(res)

        def _on_volume(self, res) -> None:
            self._results.setdefault(res.warning.id, []).append(res)
            log.info("gui.volume_done", warning=res.warning.id,
                     valid_time=res.volume.valid_time.isoformat(),
                     index=res.index, total=res.total, cells=len(res.cells),
                     selected=(res.warning.id == self._selected_id))
            if res.warning.id == self._selected_id:
                self.volumes.add_result(res)
                self.map.show_result(res.warning, res.volume, res.cells)
            self.statusBar().showMessage(
                f"{res.warning.event}  {res.volume.valid_time:%H%MZ}  "
                f"{res.index}/{res.total}  {len(res.cells)} cell(s)"
            )

        def _on_storm(self, c) -> None:
            # Drive pane 3: load that cell's volume and build the 3D scene. The
            # source factory re-grids any volume evicted from the LRU during
            # scrubbing (B2); for fixtures the registered grids serve directly.
            results = self._results.get(self._selected_id, [])
            factory = self._sources.get(self._selected_id)
            log.info("gui.storm_selected", cell_id=c.cell_id, track_id=c.track_id,
                     max_dbz=round(c.max_dbz, 1), valid_time=c.valid_time.isoformat(),
                     n_volumes=len(results))
            # Map: switch the radar layer to this cell's volume, not the last
            # one downloaded. Match by valid_time; fall back to a plain highlight.
            match = next(
                (r for r in results if r.volume.valid_time == c.valid_time), None
            )
            if match is not None:
                self.map.show_cell_selection(match.warning, match.volume, match.cells, c)
            else:
                self.map.highlight_cell(c)
            self.model.show_cell(c, results, source_factory=factory)
            self.statusBar().showMessage(
                f"Storm id {c.cell_id}  track {c.track_id}  "
                f"{c.max_dbz:.1f} dBZ  depth {c.depth_km:.1f} km"
            )

        def _on_error(self, msg: str) -> None:
            log.error("worker.error", msg=msg)
            self.statusBar().showMessage("Error: " + msg.splitlines()[0])

    return MainWindow()


def _reconcile_gui_platform() -> None:
    """Make Qt and VTK agree on a windowing system before the GUI starts.

    VTK renders through X11 whenever ``DISPLAY`` is set. On a Wayland session Qt
    defaults to the ``wayland`` platform, so the embedded VTK GL widgets (which
    drive X windows on Xwayland) cannot attach to the Wayland Qt window and the
    app aborts with BadWindow. When we detect that mismatch — a Wayland session
    with an Xwayland ``DISPLAY`` and no explicit override — pin Qt to ``xcb`` so
    both use the same X server. Honoured only if the user has not chosen a
    platform themselves.
    """
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if os.environ.get("WAYLAND_DISPLAY") and os.environ.get("DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.info("gui.platform_pinned", platform="xcb",
                 reason="wayland session with X11-only VTK")


_GRID_STACK_READY = False


def warmup_grid_stack() -> bool:
    """Import the Py-ART gridding stack on the *calling* thread (idempotent).

    Py-ART pulls in cartopy, whose module-level CRS construction calls pyproj —
    and pyproj/PROJ context initialisation is NOT thread-safe. Importing it
    lazily on a ``QThreadPool`` worker (the first grid_level2 call) segfaults, so
    we force the import onto the main thread up front. Safe to call repeatedly;
    after the first success Python's module cache makes it a no-op.
    """
    global _GRID_STACK_READY
    if _GRID_STACK_READY:
        return True
    try:
        import pyart  # noqa: F401 - imported for its side-effecting CRS init
        _GRID_STACK_READY = True
        log.info("warmup.grid_stack_ready")
    except Exception as e:  # noqa: BLE001 - live extra may be absent
        log.info("warmup.grid_stack_skipped", reason=str(e).splitlines()[0])
    return _GRID_STACK_READY


def run_gui(args: argparse.Namespace) -> int:
    _reconcile_gui_platform()
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = _build_window(persist=args.persist)
    window.show()
    # Warm the gridding stack on the main thread shortly after the window paints
    # (see warmup_grid_stack) so the first Download never imports it on a worker.
    QTimer.singleShot(200, warmup_grid_stack)
    if args.replay:
        window.load_fixtures()
    elif args.from_ and args.to:
        window.search.start_edit.setText(args.from_)
        window.search.end_edit.setText(args.to)
        window.search._emit_search()
    return app.exec()


def _ensure_display():
    """Guarantee a working, self-consistent GL context for the offscreen smoke.

    The documented invocation sets ``QT_QPA_PLATFORM=offscreen``, but VTK's
    embedded interactor still drives a real X window when a ``DISPLAY`` is
    present — so an offscreen Qt platform *fighting* VTK-on-X aborts with
    BadWindow. The fix is to make Qt and VTK agree on one display:

    * a ``DISPLAY`` is already present → render live on it under ``xcb`` (Qt and
      VTK now share the same X server). This is the common dev-box path;
    * no ``DISPLAY`` but ``Xvfb`` is available → start a private Xvfb (software
      llvmpipe) and point Qt + VTK at it;
    * neither → fall back to pure offscreen GL (EGL/OSMesa) under the offscreen
      platform; may be flaky on GL-less stacks, but nothing better is available.

    Returns the Xvfb process handle (or ``None``) so the caller can reap it.
    """
    import shutil
    import subprocess
    import time

    if os.environ.get("DISPLAY"):
        # Qt and VTK must agree on the display: the documented offscreen Qt
        # platform makes Qt windowless while VTK still drives an X window
        # (BadWindow). Switch Qt to xcb so both use the live server.
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        return None
    if not shutil.which("Xvfb"):
        # No display and no Xvfb: pure offscreen GL is the only option.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        return None
    # No display but Xvfb is available: stand up a private one for a stable,
    # self-consistent Qt + VTK context.
    for n in range(99, 120):
        sock = f"/tmp/.X11-unix/X{n}"
        if os.path.exists(sock):
            continue
        proc = subprocess.Popen(
            ["Xvfb", f":{n}", "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = f":{n}"
        os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
        os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")
        # Qt and VTK must agree on the display: with a live X server, Qt's
        # "offscreen" platform fights VTK-on-X (BadWindow), so use xcb.
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        for _ in range(200):
            if os.path.exists(sock):
                return proc
            time.sleep(0.05)
        return proc


def run_smoke(args: argparse.Namespace) -> int:
    """Build every pane, push one fixture through, exit 0 (Section 8B)."""
    xvfb = _ensure_display()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = _build_window(persist=False)
    # No window.show() — VTK renders to its own GL window; the panes are fully
    # instantiated either way.

    from .data.sites import SiteResolver
    from .data.volumes import FixtureVolumeSource
    from .data.warnings import FixtureWarningSource
    from .dialogs.download_dialog import DownloadDialog
    from .dialogs.settings_dialog import SettingsDialog
    from .pipeline import process_warning

    # Populate results from the shipped fixtures, select the first, and drive it
    # synchronously through every pane (no threads) so the run is deterministic.
    window.load_fixtures()
    fixture = Path(args.replay) if args.replay else FIXTURE_DIR / "tornado_warning_case"
    resolver = SiteResolver()
    for warning in FixtureWarningSource(fixture):
        window.on_select(warning)
        site = resolver.for_polygon(warning.polygon)
        process_warning(
            warning, FixtureVolumeSource(fixture), site, window.params,
            on_result=window._on_volume,
        )

    # Instantiate the modal dialogs to prove they build (8B).
    settings_dlg = SettingsDialog(window.dsn, window)
    download_dlg = DownloadDialog("KFWS  Tornado Warning", total=9, parent=window)
    download_dlg.update_progress(3, 9, "KFWS 1142Z")
    assert window.search.results.count() >= 1
    assert settings_dlg.model.rowCount() >= 1

    app.processEvents()
    print(
        "smoke: panes (search, volumes, map, model) + settings/download dialogs "
        "instantiated, fixture rendered — OK"
    )
    # The real smoke work is done. Reap the private Xvfb (if any) and hard-exit
    # 0: VTK/GL stacks can SIGABRT during interpreter teardown (a cosmetic
    # static-destructor race) which would otherwise flip the exit code.
    sys.stdout.flush()
    sys.stderr.flush()
    if xvfb is not None:
        xvfb.terminate()
    os._exit(0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="storm_modeler", description=__doc__)
    p.add_argument("--headless", action="store_true", help="run without a GUI")
    p.add_argument("--smoke", action="store_true", help="offscreen pane smoke test")
    p.add_argument("--replay", metavar="DIR", help="replay a fixture directory")
    p.add_argument("--persist", action="store_true", help="write to PostGIS (PG_DSN)")
    p.add_argument("--from", dest="from_", metavar="ISO", help="live IEM start (UTC)")
    p.add_argument("--to", dest="to", metavar="ISO", help="live IEM end (UTC)")
    p.add_argument("--log-level", metavar="LEVEL", default=None,
                   help="debug|info|warning|error (default info; STORM_MODELER_LOG_LEVEL)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    from .logging_setup import setup_logging

    path = setup_logging(args.log_level)
    log.info("app.start", mode=("smoke" if args.smoke else "headless"
             if args.headless else "gui"), log_file=str(path))
    if args.smoke:
        return run_smoke(args)
    if args.headless:
        return run_headless(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
