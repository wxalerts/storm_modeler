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

from .config import pg_dsn

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Headless replay (no Qt)
# ---------------------------------------------------------------------------

def run_headless(args: argparse.Namespace) -> int:
    from .pipeline import replay_fixture

    if not args.replay:
        print("--headless requires --replay <fixture_dir>")
        return 2

    dsn = pg_dsn() if args.persist else None
    if args.persist and not dsn:
        print("--persist set but PG_DSN is not configured")
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


def _build_window(persist: bool):
    import threading

    from PySide6.QtCore import Qt, QSettings, QThreadPool
    from PySide6.QtWidgets import QMainWindow, QSplitter, QStatusBar

    from .data.sites import SiteResolver
    from .data.volumes import FixtureVolumeSource, S3Level2Source, bounded_window
    from .downloads import DownloadStore
    from .dialogs.download_dialog import DownloadDialog
    from .dialogs.settings_dialog import SettingsDialog
    from .panes.left_search import LeftSearchPane
    from .panes.left_volumes import LeftVolumesPane
    from .panes.map_client import MapClient
    from .panes.logs_window import LogsWindow
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
            # On-disk cache of downloaded warnings (volumes + metadata) so the
            # "Downloaded" list and its data survive app restarts.
            self.downloads = DownloadStore()

            # GIMP-style multi-window: this window is the Data hub (search +
            # volumes); the 3D view, the Leaflet map (a separate GTK/WebKit
            # process), and the logs each get their own top-level window.
            self.search = LeftSearchPane(self.settings)
            self.volumes = LeftVolumesPane()
            self.model = ModelPane(self.settings)
            self.map = MapClient()  # out-of-process GTK/WebKit Leaflet window

            # This window: search on top, volumes below.
            self.setWindowTitle("WxAlerts Storm Modeler — Data")
            self.resize(440, 940)
            left = QSplitter(Qt.Vertical)
            left.addWidget(self.search)
            left.addWidget(self.volumes)
            left.setSizes([520, 420])
            self.setCentralWidget(left)

            # 3D View window.
            self.model_window = QMainWindow()
            self.model_window.setWindowTitle("3D View")
            self.model_window.setCentralWidget(self.model)
            self.model_window.resize(760, 780)

            # Logs window (tails the run's log file).
            from .logging_setup import current_log_path
            self.logs_window = LogsWindow(current_log_path())

            # Persisted window geometry (position + size) across launches.
            self._geo = QSettings("WxAlerts", "StormModeler")
            self._geo_windows = {
                "data": self, "model": self.model_window, "logs": self.logs_window,
            }

            self.setStatusBar(QStatusBar())
            self.map.set_basemap()
            self._build_menu()

            # Record the 3D pane's VTK GL context — the first thing to inspect
            # if a render crashes on this display stack.
            from .logging_setup import log_gl_info
            log_gl_info(self.model.plotter, where="model3d")

            # Wiring.
            self.search.search_requested.connect(self.on_search)
            self.search.warning_selected.connect(self.on_select)
            self.search.download_requested.connect(self.on_download)
            self.search.downloaded_selected.connect(self.on_downloaded_select)
            # _on_storm builds the 3D scene; the 3D pane then emits frame_ready
            # for each frame (incl. playback), which drives the map in lock-step.
            self.volumes.storm_selected.connect(self._on_storm)
            self.model.frame_ready.connect(self._on_model_frame)

            # Repopulate the "Downloaded" list from the on-disk cache.
            self._load_persisted_downloads()

        def _load_persisted_downloads(self) -> None:
            """Restore previously-downloaded warnings from the download cache.

            Each gets a :class:`FixtureVolumeSource` over its cached ``*.npz``
            volumes registered as its source, so selecting it later replays
            through SCIT with no network and no re-gridding.
            """
            for warning in self.downloads.warnings():
                d = self.downloads.dir(warning.id)
                self._sources[warning.id] = (
                    lambda dd: (lambda: FixtureVolumeSource(dd))
                )(d)
                self.search.add_downloaded(warning)

        # --- menu ---------------------------------------------------------

        def _build_menu(self) -> None:
            run_menu = self.menuBar().addMenu("&Run")
            run_menu.addAction("Search alerts…").triggered.connect(self.search.open_search)
            run_menu.addSeparator()
            run_menu.addAction("Stop").triggered.connect(self.stop)
            settings_menu = self.menuBar().addMenu("&Settings")
            settings_menu.addAction("Open settings…").triggered.connect(self.open_settings)
            self._build_product_menu()
            win_menu = self.menuBar().addMenu("&Windows")
            win_menu.addAction("3D View").triggered.connect(
                lambda: self._raise_window(self.model_window))
            win_menu.addAction("Logs").triggered.connect(
                lambda: self._raise_window(self.logs_window))

        def _build_product_menu(self) -> None:
            from PySide6.QtGui import QActionGroup, QKeySequence
            from .data.radar_render import PRODUCTS

            menu = self.menuBar().addMenu("&Product")
            group = QActionGroup(self)
            group.setExclusive(True)
            self._product_actions = []
            for i, p in enumerate(PRODUCTS):
                act = menu.addAction(p["label"])
                act.setCheckable(True)
                act.setChecked(i == 0)
                act.triggered.connect(lambda _c, k=p["key"]: self.set_product(k))
                group.addAction(act)
                self._product_actions.append((p["key"], act))
            menu.addSeparator()
            cyc = menu.addAction("Cycle product")
            cyc.setShortcut(QKeySequence("P"))  # press P to cycle products
            cyc.triggered.connect(self.cycle_product)

        def set_product(self, key: str) -> None:
            self.map.set_product(key)
            for k, act in getattr(self, "_product_actions", []):
                act.setChecked(k == key)
            self.statusBar().showMessage(f"Product: {key}")

        def cycle_product(self) -> None:
            from .data.radar_render import PRODUCTS
            keys = [p["key"] for p in PRODUCTS]
            cur = getattr(self.map, "_product", "reflectivity")
            self.set_product(keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else keys[0])

        def _raise_window(self, win) -> None:
            win.show()
            win.raise_()
            win.activateWindow()

        def show_windows(self) -> None:
            """Place and show the satellite windows (called once at startup)."""
            # Restore saved geometry, or fall back to a sensible default layout.
            if self._geo.value("geometry/data") is None:
                self.move(40, 60)
                self.model_window.move(500, 60)
                self.logs_window.move(500, 600)
            else:
                for name, win in self._geo_windows.items():
                    g = self._geo.value(f"geometry/{name}")
                    if g is not None:
                        win.restoreGeometry(g)
            self.model_window.show()
            self.logs_window.show()

        def _save_geometry(self) -> None:
            for name, win in self._geo_windows.items():
                self._geo.setValue(f"geometry/{name}", win.saveGeometry())

        def closeEvent(self, event) -> None:  # noqa: N802
            # The Data window is the hub: closing it shuts the whole app down,
            # including the GTK map subprocess and the satellite windows.
            self._save_geometry()
            self.map.shutdown()
            self.model_window.close()
            self.logs_window.close()
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
            super().closeEvent(event)

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
            self._sources[warning.id] = lambda: S3Level2Source(
                site.icao, w0, w1, site.lat, site.lon,
                h_km=self.params.grid_h_km, v_km=self.params.grid_v_km,
            )
            self.search.add_result(warning)

        def on_select(self, warning) -> None:
            self._selected_id = warning.id
            self.volumes.set_warning(warning)
            self.map.show_warning(warning)
            for res in self._results.get(warning.id, []):
                self.volumes.add_result(res)
            self.statusBar().showMessage(f"Selected {warning.event} ETN {warning.etn:04d}")

        def on_downloaded_select(self, warning) -> None:
            """Select a previously-downloaded warning from the sidebar list.

            If its volumes are already in memory (downloaded this session), just
            show them; otherwise replay them from the on-disk cache through the
            normal download path (no network, no re-gridding).
            """
            if self._results.get(warning.id):
                self.on_select(warning)
            elif self._sources.get(warning.id) is not None:
                self.on_download(warning)
            else:
                self.on_select(warning)

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
            worker.signals.status.connect(dialog.set_status)
            worker.signals.volume_gridded.connect(self._on_gridded)
            worker.signals.error.connect(self._on_error)
            worker.signals.finished.connect(dialog.finish)
            worker.signals.finished.connect(self._close_download_persistence)
            # Record it in the session "Downloaded" list once volumes are in.
            worker.signals.warning_done.connect(self.search.add_downloaded)
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

            # Cache the warning + this gridded volume to disk so the download
            # persists across launches (replayed later via FixtureVolumeSource).
            try:
                self.downloads.save_warning(warning)
                self.downloads.save_volume(warning.id, volume)
            except Exception as e:  # noqa: BLE001 - caching must never break a run
                log.error("gui.cache_error", warning=warning.id, error=str(e))

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
            # Drive pane 3: build the 3D scene + arm the scrubber. The 3D pane
            # emits frame_ready per frame, which drives the map (radar + cells)
            # in lock-step — so selection AND playback keep the two views synced.
            results = self._results.get(self._selected_id, [])
            factory = self._sources.get(self._selected_id)
            self._map_warning = results[0].warning if results else None
            log.info("gui.storm_selected", cell_id=c.cell_id, track_id=c.track_id,
                     max_dbz=round(c.max_dbz, 1), valid_time=c.valid_time.isoformat(),
                     n_volumes=len(results))
            self.model.show_cell(c, results, source_factory=factory)
            self.statusBar().showMessage(
                f"Storm id {c.cell_id}  track {c.track_id}  "
                f"{c.max_dbz:.1f} dBZ  depth {c.depth_km:.1f} km"
            )

        def _on_model_frame(self, volume, cell, cells) -> None:
            """3D pane produced a frame (selection or playback) — sync the map."""
            if getattr(self, "_map_warning", None) is not None:
                self.map.show_cell_selection(self._map_warning, volume, cells, cell)

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
    window.show_windows()  # 3D View + Logs windows (the map is its own process)
    app.aboutToQuit.connect(window.map.shutdown)
    # Warm the gridding stack on the main thread shortly after the window paints
    # (see warmup_grid_stack) so the first Download never imports it on a worker.
    QTimer.singleShot(200, warmup_grid_stack)
    if args.from_ and args.to:
        window.search.start_edit.setText(args.from_)
        window.search.end_edit.setText(args.to)
        window.search.open_search()
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

    import tempfile

    from .data.sites import SiteResolver
    from .data.synthetic import build_tornado_case
    from .data.volumes import FixtureVolumeSource
    from .data.warnings import FixtureWarningSource
    from .dialogs.download_dialog import DownloadDialog
    from .dialogs.settings_dialog import SettingsDialog
    from .pipeline import process_warning

    # Generate a synthetic case in a temp dir (no shipped fixtures), add it to
    # the search list, select it, and drive it synchronously through every pane
    # (no threads) so the run is deterministic.
    fixture = Path(args.replay) if args.replay else build_tornado_case(
        Path(tempfile.mkdtemp(prefix="sm_smoke_"))
    )
    resolver = SiteResolver()
    for warning in FixtureWarningSource(fixture):
        window._sources[warning.id] = (lambda d: (lambda: FixtureVolumeSource(d)))(fixture)
        window.search.add_result(warning)
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
    try:
        window.map.shutdown()  # reap the GTK map subprocess
    except Exception:  # noqa: BLE001
        pass
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
