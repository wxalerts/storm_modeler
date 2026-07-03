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

from .config import (
    IEM_SEARCH_MODULES,
    LIVE_EXTRA_HINT,
    local_settings_path,
    missing_modules,
    pg_dsn,
)

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


def _build_window(persist: bool, *, use_local_settings: bool = True):
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
    from .panes.hrrr_window import HRRRWindow
    from .panes.lightning_window import LightningWindow
    from .panes.logs_window import LogsWindow
    from .panes.model_view import ModelPane
    from .panes.satellite_pane import SatelliteWindow
    from .panes.stormtrack_window import StormTrackWindow
    from .settings.resolver import resolve
    from .workers import (
        HRRRWorker,
        LightningWorker,
        SatelliteWorker,
        SearchWorker,
        WarningWorker,
    )

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("WxAlerts Storm Modeler — Phase A")
            self.resize(1600, 950)

            self.dsn = pg_dsn() if persist else None
            # No database? Persist tuning to a local JSON file instead, so the
            # Settings dialog survives restarts. ``--persist`` only governs the
            # PostGIS DSN; the local-file fallback is the default for the GUI so
            # tuning sticks without a database. (The smoke test passes
            # use_local_settings=False to stay pure: no DSN, no file.)
            self.settings_path = (
                local_settings_path() if (use_local_settings and not self.dsn) else None
            )
            self.settings = resolve(self.dsn, self.settings_path)
            self.params = self.settings.detection
            self.resolver = SiteResolver()
            self.pool = QThreadPool.globalInstance()
            self._workers: list = []
            self._sources: dict[str, object] = {}   # warning.id -> () -> VolumeSource
            self._results: dict[str, list] = {}      # warning.id -> [VolumeResult]
            self._selected_id: str | None = None
            self._selected_warning = None  # the warning object behind _selected_id
            # GLM lightning for the selected warning: all fetched flashes, their
            # full time span (for a stable colour ramp), and the valid_time of
            # the volume currently on the map (so only that volume's strikes show
            # and advance in lock-step with playback).
            self._lightning_flashes: list = []
            self._lightning_span: tuple[float, float] | None = None
            self._current_valid_time = None
            # Satellite cloud-top state for the selected warning.
            self._sat_results: list = []
            self._sat_tracker = None
            self._sat_persistence = None
            self._current_sat_time = None  # valid_time of the scene on the map
            # HRRR freezing-level grids for the selected warning (vault/OT
            # detection re-runs against them whenever a volume or grid lands).
            self._hrrr_grids: list = []
            # Last (cell, cells) the SRM motion frame was resolved from, so a
            # settings reload can re-resolve without a fresh selection.
            self._srm_context: tuple = (None, None)
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

            # Lightning window (fetches GOES GLM flashes onto the map).
            self.lightning_window = LightningWindow()

            # Satellite window (fetches GOES ABI cloud-top analysis onto the map).
            self.satellite_window = SatelliteWindow()

            # Environment window (fetches the HRRR 0°C level, drives vault/OT).
            self.hrrr_window = HRRRWindow()

            # Storm Track window (lists tracked storms + charts their history).
            self.stormtrack_window = StormTrackWindow()

            # Persisted window geometry (position + size) across launches.
            self._geo = QSettings("WxAlerts", "StormModeler")
            self._geo_windows = {
                "data": self, "model": self.model_window, "logs": self.logs_window,
                "lightning": self.lightning_window,
                "satellite": self.satellite_window,
                "hrrr": self.hrrr_window,
                "stormtrack": self.stormtrack_window,
            }

            self.setStatusBar(QStatusBar())
            self.map.set_basemap()
            self._build_menu()

            # Record the 3D pane's VTK GL context — the first thing to inspect
            # if a render crashes on this display stack.
            from .logging_setup import log_gl_info
            log_gl_info(self.model.xplotter, where="model_xsection")

            # Wiring.
            self.search.search_requested.connect(self.on_search)
            self.search.warning_selected.connect(self.on_select)
            self.search.download_requested.connect(self.on_download)
            self.search.downloaded_selected.connect(self.on_downloaded_select)
            # _on_storm builds the 3D scene; the 3D pane then emits frame_ready
            # for each frame (incl. playback), which drives the map in lock-step.
            self.volumes.storm_selected.connect(self._on_storm)
            self.volumes.couplet_selected.connect(self._on_couplet_selected)
            self.model.frame_ready.connect(self._on_model_frame)
            self.lightning_window.fetch_requested.connect(self.on_fetch_lightning)
            self.lightning_window.clear_requested.connect(self.map.clear_lightning)
            self.satellite_window.fetch_requested.connect(self.on_fetch_satellite)
            self.satellite_window.clear_requested.connect(self.map.clear_satellite)
            self.satellite_window.opacity_changed.connect(self.map.set_satellite_opacity)
            self.hrrr_window.fetch_requested.connect(self.on_fetch_hrrr)
            self.hrrr_window.clear_requested.connect(self._clear_hrrr)
            self.stormtrack_window.track_selected.connect(self._on_track_selected)
            self.stormtrack_window.clear_requested.connect(self.map.clear_stormtrack)

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
            win_menu.addAction("Lightning").triggered.connect(
                lambda: self._raise_window(self.lightning_window))
            win_menu.addAction("Satellite").triggered.connect(
                lambda: self._raise_window(self.satellite_window))
            win_menu.addAction("Environment (HRRR)").triggered.connect(
                lambda: self._raise_window(self.hrrr_window))
            win_menu.addAction("Storm Tracks").triggered.connect(
                lambda: self._raise_window(self.stormtrack_window))
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
                self.lightning_window.move(40, 620)
                self.satellite_window.move(40, 880)
                self.hrrr_window.move(420, 880)
                self.stormtrack_window.move(1280, 60)
            else:
                for name, win in self._geo_windows.items():
                    g = self._geo.value(f"geometry/{name}")
                    if g is not None:
                        win.restoreGeometry(g)
            self.model_window.show()
            self.logs_window.show()
            self.lightning_window.show()
            self.satellite_window.show()
            self.hrrr_window.show()
            self.stormtrack_window.show()

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
            self.lightning_window.close()
            self.satellite_window.close()
            self.hrrr_window.close()
            self.stormtrack_window.close()
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()
            super().closeEvent(event)

        def open_settings(self) -> None:
            dlg = SettingsDialog(self.dsn, self.settings_path, self)
            dlg.settingsChanged.connect(self.reload_settings)
            dlg.exec()

        def reload_settings(self) -> None:
            self.settings = resolve(self.dsn, self.settings_path)
            self.params = self.settings.detection
            self.model.set_settings(self.settings)
            self.statusBar().showMessage(
                f"Settings reloaded (detection hash {self.params.settings_hash})"
            )
            self._reprocess_current()
            # SRM motion source / manual vector may have changed — re-resolve
            # from the last selection context and re-push to the map.
            self._update_srm_motion(*self._srm_context)

        def _reprocess_current(self) -> None:
            """Re-run SCIT detection + tracking on the selected warning, in memory.

            Triggered live from the settings dialog (debounced, as the user edits)
            and on its "Save and Reprocess" button. Runs entirely against the
            already-gridded volumes cached in ``_results`` — no network, no
            re-gridding, no download dialog — so a settings tweak re-detects in
            near real time. Skips silently if nothing is loaded.
            """
            wid = self._selected_id
            results = self._results.get(wid) if wid else None
            if not results:
                return

            from .detection.detection_v2 import Tracker
            from .detection.detection_v2 import run as scit_run
            from .pipeline import VolumeResult, annotate_vault_results

            # Tracking is stateful across the volume sequence, so re-run the whole
            # warning in chronological order through a fresh tracker.
            tracker = Tracker(self.params)
            rebuilt: list = []
            for res in sorted(results, key=lambda r: r.volume.valid_time):
                try:
                    cells = scit_run(res.volume, self.params)
                    tracker.update(cells, res.volume.valid_time)
                except Exception as e:  # noqa: BLE001
                    log.error("gui.reprocess_error", warning=wid, error=str(e))
                    return
                rebuilt.append(VolumeResult(
                    warning=res.warning, site=res.site, volume=res.volume,
                    cells=cells, index=res.index, total=res.total,
                    settings_hash=self.params.settings_hash,
                ))
            # Fresh cells lost their vault annotation — re-derive it from the
            # already-fetched freezing-level grids (no-op without any).
            annotate_vault_results(rebuilt, self._hrrr_grids, self.settings.vault)
            # Couplets follow the cells' lifecycle: recompute per volume with
            # the full rebuilt history (track motion needs it).
            for res in rebuilt:
                self._annotate_couplets(res, rebuilt)
            self._results[wid] = rebuilt

            # Refresh the detection-driven views; leave lightning/satellite
            # overlays (independent of detection params) untouched.
            self.volumes.show_results(rebuilt)
            last = rebuilt[-1]
            self.map.show_result(last.warning, last.volume, last.cells)
            self.map.set_couplets(last.couplets, self._couplet_marker_min_kt())
            self._refresh_tracks()
            self.statusBar().showMessage(
                f"Reprocessed {last.warning.event} — {len(rebuilt)} volume(s) "
                f"(detection hash {self.params.settings_hash})"
            )

        # --- search / results --------------------------------------------

        def on_search(self, params) -> None:
            self.search.clear_results()
            phenomena = ([] + (["TO"] if params.tornado else [])
                         + (["SV"] if params.severe else []))
            if not phenomena:
                self.statusBar().showMessage("Select at least one event type.")
                return
            missing = missing_modules(IEM_SEARCH_MODULES)
            if missing:
                self.statusBar().showMessage(
                    f"IEM search needs the live extra ({', '.join(missing)} "
                    f"missing) — {LIVE_EXTRA_HINT}"
                )
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
            self._selected_warning = warning
            self.volumes.set_warning(warning)
            self.map.show_warning(warning)
            # Arm the lightning + satellite windows for this warning and drop any
            # overlays/state left over from the previous one.
            self.lightning_window.set_warning(warning)
            self._reset_lightning()
            self.satellite_window.set_warning(warning)
            self._reset_satellite()
            self.hrrr_window.set_warning(warning)
            self._reset_hrrr()
            self.stormtrack_window.set_warning(warning)
            self.map.clear_stormtrack()
            self.map.clear_couplets()
            for res in self._results.get(warning.id, []):
                self.volumes.add_result(res)
            self._refresh_tracks()
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
            self._selected_warning = warning
            self._results[warning.id] = []
            self.volumes.set_warning(warning)
            self.map.show_warning(warning)
            self.lightning_window.set_warning(warning)
            self._reset_lightning()
            self.satellite_window.set_warning(warning)
            self._reset_satellite()
            self.hrrr_window.set_warning(warning)
            self._reset_hrrr()
            self.stormtrack_window.set_warning(warning)
            self.map.clear_stormtrack()
            self.map.clear_couplets()

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
            # Optionally chain a GOES cloud-top pull once all volumes are in
            # (off by default; see the sat_auto_fetch setting).
            worker.signals.finished.connect(
                lambda w=warning: self._maybe_auto_satellite(w)
            )
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
            from .pipeline import VolumeResult, annotate_vault_results

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
            # Vault/OT annotation, if the freezing level is already in (no-op
            # otherwise — a later HRRR fetch re-annotates every volume).
            annotate_vault_results([res], self._hrrr_grids, self.settings.vault)
            self._annotate_couplets(res, self._results.get(warning.id, []) + [res])
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
                self.map.set_couplets(res.couplets, self._couplet_marker_min_kt())
                self._show_lightning_for(res.volume)
                self._show_satellite_for(res.volume)
                self._show_hrrr_for(res.volume)
                self._refresh_tracks()
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
            # Frame SRM on this storm's track motion (fallbacks inside).
            volume_cells = next(
                (r.cells for r in results if r.volume.valid_time == c.valid_time), []
            )
            self._update_srm_motion(c, volume_cells)
            self.model.show_cell(c, results, source_factory=factory)
            self.statusBar().showMessage(
                f"Storm id {c.cell_id}  track {c.track_id}  "
                f"{c.max_dbz:.1f} dBZ  depth {c.depth_km:.1f} km"
            )

        # --- rotation couplets ---------------------------------------------

        def _couplet_params(self):
            """The rotation-detection knob set, resolved from settings."""
            return self.settings.couplets

        def _couplet_marker_min_kt(self) -> float:
            return self.settings.couplet_marker_min_vrot_kt

        def _annotate_couplets(self, res, results) -> None:
            """Detect this volume's velocity couplets (never fatal to the run).

            Rides the same lifecycle as SCIT cells: per fresh volume in
            ``_on_gridded`` and per rebuilt volume in ``_reprocess_current``.
            ``results`` is the track history the motion association reads.
            """
            from .detection.couplets import detect_couplets

            try:
                res.couplets = detect_couplets(
                    res.volume, res.cells, self._couplet_params(),
                    results=results,
                )
            except Exception as e:  # noqa: BLE001 - annotation must not break a run
                log.error("gui.couplet_error", warning=res.warning.id,
                          error=str(e))
                res.couplets = []

        def _on_couplet_selected(self, cp) -> None:
            """A couplet row was clicked in the Data pane — pan to its marker."""
            self.map.pan_to(cp.centroid_lat, cp.centroid_lon)
            self.statusBar().showMessage(
                f"{'Cyclonic' if cp.cyclonic else 'Anticyclonic'} rotation  "
                f"Vrot {cp.vr_sr_ms * 1.94384:.0f} kt SR  "
                f"r {cp.range_km:.0f} km @ {cp.center_az_deg:03.0f}°"
            )

        # --- storm tracks -------------------------------------------------

        def _refresh_tracks(self) -> None:
            """Rebuild the Storm Track listing for the selected warning.

            Cheap and pure (:func:`build_tracks`), so it runs on every new
            volume and after each satellite association; the window preserves the
            user's current track selection across the rebuild.
            """
            from .tracks import build_tracks

            results = self._results.get(self._selected_id, [])
            self.stormtrack_window.set_tracks(build_tracks(results))

        def _on_track_selected(self, track) -> None:
            """A track was clicked in the Storm Track window — draw it on the map."""
            if track is None:
                return
            self.map.show_storm_track(track)
            # Frame SRM on the selected track: use its latest cell as the
            # motion anchor (the segment walk needs a StormCell + results).
            results = self._results.get(self._selected_id, [])
            anchor = None
            for res in sorted(results, key=lambda r: r.volume.valid_time):
                for c in res.cells:
                    if track.track_id >= 0 and c.track_id == track.track_id:
                        anchor = (c, res.cells)
            if anchor is not None:
                self._update_srm_motion(*anchor)

        def _update_srm_motion(self, cell=None, cells=None) -> None:
            """Resolve the SRM motion frame per settings and push it to the map.

            Runs on storm/track selection and on every scrub frame, so the
            frame follows the track segment under the displayed volume.
            Resolution order (``srm_motion_source``): manual vector →
            selected track segment → mean motion of the tracks alive in the
            current volume → (0, 0), where SRM degrades to base velocity; any
            drop down the chain logs ``srm.motion_fallback``.
            """
            from .viz import motion

            self._srm_context = (cell, cells)
            results = self._results.get(self._selected_id, [])
            source = str(self.settings.get("srm_motion_source", "selected_track"))
            manual_uv = None
            if source == "manual":
                manual_uv = motion.uv_from_speed_dir(
                    float(self.settings.get("srm_manual_speed_kt", 30.0)),
                    float(self.settings.get("srm_manual_dir_deg", 240.0)),
                )
            (u, v), used = motion.resolve_motion(source, cell, cells, results,
                                                 manual_uv)
            if used != source:
                log.warning("srm.motion_fallback", requested=source, used=used,
                            u_ms=round(u, 2), v_ms=round(v, 2))
            self.map.set_storm_motion(u, v)

        def _on_model_frame(self, volume, cell, cells) -> None:
            """3D pane produced a frame (selection or playback) — sync the map."""
            if getattr(self, "_map_warning", None) is not None:
                # Re-derive the SRM frame from this frame's track segment
                # BEFORE the map re-renders, so the frame follows the track.
                self._update_srm_motion(cell, cells)
                self.map.show_cell_selection(self._map_warning, volume, cells, cell)
                # Keep the rotation markers in step with the displayed volume.
                res = next(
                    (r for r in self._results.get(self._selected_id, [])
                     if r.volume.valid_time == volume.valid_time), None,
                )
                self.map.set_couplets(res.couplets if res else [],
                                      self._couplet_marker_min_kt())
                self._show_lightning_for(volume)
                self._show_satellite_for(volume)
                self._show_hrrr_for(volume)

        def _on_error(self, msg: str) -> None:
            log.error("worker.error", msg=msg)
            self.statusBar().showMessage("Error: " + msg.splitlines()[0])

        # --- lightning ----------------------------------------------------

        def on_fetch_lightning(self, warning) -> None:
            """Pull GOES GLM flashes for *warning*'s window onto the map."""
            from .config import LIGHTNING_MODULES
            from .data.lightning import bucket_for_date
            from .data.volumes import bounded_window

            missing = missing_modules(LIGHTNING_MODULES)
            if missing:
                self.lightning_window.set_busy(False)
                self.lightning_window.set_status(
                    f"Lightning needs the live extra ({', '.join(missing)} "
                    f"missing) — {LIVE_EXTRA_HINT}"
                )
                return

            w0, w1 = bounded_window(
                warning.issued, warning.expires,
                self.settings.pre_minutes, self.settings.post_minutes,
            )
            lon0, lat0, lon1, lat1 = warning.polygon.bounds
            pad = float(self.settings.get("lightning_bbox_pad_deg", 0.5))
            bbox = (lat0 - pad, lon0 - pad, lat1 + pad, lon1 + pad)
            bucket, sat = bucket_for_date(w0.date())
            log.info("lightning.fetch", warning=warning.id, bucket=bucket,
                     start=w0.isoformat(), end=w1.isoformat())
            self.lightning_window.set_status(f"{sat}: {w0:%H:%MZ}–{w1:%H:%MZ}…")

            cancel = threading.Event()
            worker = LightningWorker(
                w0, w1, bbox, bucket=bucket,
                max_flashes=int(self.settings.get("lightning_max_flashes", 5000)),
                good_only=bool(self.settings.get("lightning_quality_good_only", True)),
                cancel=cancel,
            )
            worker.signals.status.connect(self.lightning_window.set_status)
            worker.signals.flashes.connect(self._on_lightning)
            worker.signals.error.connect(
                lambda m: self.lightning_window.set_status("Error: " + m.splitlines()[0])
            )
            worker.signals.finished.connect(lambda: self.lightning_window.set_busy(False))
            self._workers.append(worker)
            self.pool.start(worker)

        def _on_lightning(self, flashes) -> None:
            """Store the event's flashes; show only the current volume's slice."""
            self._lightning_flashes = list(flashes)
            if flashes:
                times = [f.time for f in flashes]
                self._lightning_span = (min(times).timestamp(), max(times).timestamp())
                self.lightning_window.set_time_range(min(times), max(times))
            else:
                self._lightning_span = None
                self.lightning_window.set_time_range(None, None)
            if self._current_valid_time is None:
                vts = sorted(r.volume.valid_time
                             for r in self._results.get(self._selected_id, []))
                self._current_valid_time = vts[0] if vts else None
            self._refresh_lightning()

        def _show_lightning_for(self, volume) -> None:
            """Remember the volume now on the map and refresh its strike layer."""
            self._current_valid_time = volume.valid_time
            if self._lightning_flashes:
                self._refresh_lightning()

        def _refresh_lightning(self) -> None:
            total = len(self._lightning_flashes)
            if not total or self._current_valid_time is None:
                self.map.clear_lightning()
                self.lightning_window.set_count(0, total=total)
                return
            subset = self._lightning_for_volume(self._current_valid_time)
            self.map.show_lightning(subset, span=self._lightning_span)
            self.lightning_window.set_count(len(subset), total=total)

        def _lightning_for_volume(self, valid_time) -> list:
            from .data.lightning import bin_nearest

            volume_times = [r.volume.valid_time
                            for r in self._results.get(self._selected_id, [])]
            return bin_nearest(self._lightning_flashes, valid_time, volume_times)

        def _reset_lightning(self) -> None:
            self._lightning_flashes = []
            self._lightning_span = None
            self._current_valid_time = None
            self.map.clear_lightning()

        # --- environment (HRRR freezing level / vault) ---------------------

        def _reset_hrrr(self) -> None:
            """Drop the previous warning's freezing-level grids (view switch)."""
            self._hrrr_grids = []

        def _clear_hrrr(self) -> None:
            """Clear button: drop the grids AND strip the vault annotations."""
            self._hrrr_grids = []
            for res in self._results.get(self._selected_id, []):
                for c in res.cells:
                    c.freezing_level_km = None
                    c.vault_top_km = None
                    c.vault_depth_km = None
                    c.overshooting_top = False
                self.volumes.add_result(res)
            self._refresh_tracks()
            self.hrrr_window.count_label.setText("")
            self.hrrr_window.set_level("")
            self.hrrr_window.set_status("Cleared.")

        def _vault_cell_count(self) -> int:
            return sum(
                1 for r in self._results.get(self._selected_id, [])
                for c in r.cells if c.overshooting_top
            )

        def on_fetch_hrrr(self, warning) -> None:
            """Pull HRRR 0°C freezing-level grids and run vault/OT detection."""
            import math

            from .config import DEFAULT_GRID, HRRR_MODULES
            from .data.hrrr import HRRRSource
            from .data.radar_render import geo_bounds
            from .data.volumes import bounded_window

            missing = missing_modules(HRRR_MODULES)
            if missing:
                self.hrrr_window.set_busy(False)
                self.hrrr_window.set_status(
                    f"HRRR needs the live extra ({', '.join(missing)} "
                    f"missing) — {LIVE_EXTRA_HINT}"
                )
                return

            w0, w1 = bounded_window(
                warning.issued, warning.expires,
                self.settings.pre_minutes, self.settings.post_minutes,
            )
            # Crop the freezing-level grid to the NEXRAD volume footprint (the
            # radar analysis-grid extent), same as the satellite pull, so every
            # detected cell samples inside the raster.
            site = self.resolver.for_polygon(warning.polygon)
            pad = self.settings.hrrr_bbox_pad_deg
            results = self._results.get(warning.id) or []
            if results:
                lon_min, lon_max, lat_min, lat_max = geo_bounds(results[0].volume)
                bbox = (lon_min - pad, lat_min - pad, lon_max + pad, lat_max + pad)
            else:
                half = DEFAULT_GRID.half_width_km
                dlat = half / 110.574
                dlon = half / (111.320 * max(0.1, math.cos(math.radians(site.lat))))
                bbox = (site.lon - dlon - pad, site.lat - dlat - pad,
                        site.lon + dlon + pad, site.lat + dlat + pad)

            self._hrrr_grids = []
            if not results:
                self.hrrr_window.set_status(
                    f"HRRR: {w0:%H:%MZ}–{w1:%H:%MZ} — no radar yet, vault "
                    "detection runs once volumes are in."
                )
            else:
                self.hrrr_window.set_status(f"HRRR: {w0:%H:%MZ}–{w1:%H:%MZ}…")

            log.info("hrrr.fetch", warning=warning.id,
                     start=w0.isoformat(), end=w1.isoformat())
            cancel = threading.Event()
            source = HRRRSource(
                w0, w1, bbox, target_res_deg=self.settings.hrrr_target_res_deg,
            )
            worker = HRRRWorker(warning, source, cancel)
            worker.signals.status.connect(self.hrrr_window.set_status)
            worker.signals.grid_ready.connect(self._on_hrrr_grid)
            worker.signals.error.connect(
                lambda m: self.hrrr_window.set_status("Error: " + m.splitlines()[0])
            )
            worker.signals.finished.connect(lambda: self.hrrr_window.set_busy(False))
            self._workers.append(worker)
            self.pool.start(worker)

        def _on_hrrr_grid(self, warning, grid, index, total) -> None:
            """GUI-thread vault detection + refresh for one freezing-level grid.

            The worker already downloaded and resampled the grid; here we cache
            it, re-run vault detection over the warning's volumes (which needs
            the in-memory radar results, hence this thread), and refresh the
            listings so the vault/OT segments appear.
            """
            from .pipeline import annotate_vault_results

            try:
                self.downloads.save_warning(warning)
                self.downloads.save_flevel(warning.id, grid)
            except Exception as e:  # noqa: BLE001 - caching must never break a run
                log.error("gui.flevel_cache_error", warning=warning.id, error=str(e))

            self._hrrr_grids.append(grid)
            self._hrrr_grids.sort(key=lambda g: g.valid_time)

            if warning.id != self._selected_id:
                return
            results = self._results.get(warning.id, [])
            if results:
                annotate_vault_results(results, self._hrrr_grids, self.settings.vault)
                for res in results:
                    self.volumes.add_result(res)
                self._refresh_tracks()
            self.hrrr_window.set_count(len(self._hrrr_grids), self._vault_cell_count())
            self._show_hrrr_for()
            self.statusBar().showMessage(
                f"{warning.event}  HRRR 0°C level {grid.valid_time:%H%MZ}  "
                f"{index}/{total}"
            )

        def _show_hrrr_for(self, volume=None) -> None:
            """Label the freezing level for the radar volume now on the map.

            Cheap: picks the grid nearest the displayed volume's time and shows
            its 0°C height at the warning centroid (raster mean as fallback), so
            the Environment window tracks playback like the other overlays.
            """
            import math

            if not self._hrrr_grids:
                return
            t = volume.valid_time if volume is not None else self._current_valid_time
            if t is None:
                grid = self._hrrr_grids[-1]
            else:
                grid = min(
                    self._hrrr_grids,
                    key=lambda g: abs((g.valid_time - t).total_seconds()),
                )
            level_m = float("nan")
            if self._selected_warning is not None:
                lon, lat = self._selected_warning.centroid
                level_m = grid.height_at(lon, lat)
            if not math.isfinite(level_m):
                level_m = grid.mean_height_m()
            if math.isfinite(level_m):
                self.hrrr_window.set_level(
                    f"0°C level {level_m / 1000.0:.1f} km MSL @ {grid.valid_time:%H:%MZ}"
                )

        # --- satellite (GOES ABI cloud tops) ------------------------------

        def _reset_satellite(self) -> None:
            self._sat_results = []  # [(SatelliteScene, list[CloudTopCell])]
            self._sat_tracker = None
            self._sat_persistence = None
            self._current_sat_time = None
            self.map.clear_satellite()

        def _show_satellite_for(self, volume=None) -> None:
            """Overlay the cloud-top scene nearest in time to the displayed volume.

            Keeps the satellite layer locked to the radar volume currently on the
            map (selection or 3D playback), so the two views always show the same
            moment. With no volume on screen yet, falls back to the latest scene.
            """
            if not self._sat_results:
                return
            t = volume.valid_time if volume is not None else self._current_valid_time
            if t is None:
                scene, cells = self._sat_results[-1]
            else:
                scene, cells = min(
                    self._sat_results,
                    key=lambda sc: abs((sc[0].valid_time - t).total_seconds()),
                )
            if scene.valid_time == self._current_sat_time:
                return  # already showing this scene
            self._current_sat_time = scene.valid_time
            self.map.show_satellite(scene, cells)

        def _maybe_auto_satellite(self, warning) -> None:
            """Chain a cloud-top pull after a radar download, if enabled.

            Off by default (``sat_auto_fetch``). Only fires for the warning still
            selected, so background downloads don't hijack the view.
            """
            if not bool(self.settings.get("sat_auto_fetch", False)):
                return
            if warning.id != self._selected_id:
                return
            self.satellite_window.set_busy(True)
            self.on_fetch_satellite(warning)

        def on_fetch_satellite(self, warning) -> None:
            """Ingest GOES ABI scenes for *warning*, detect cloud tops, overlay them."""
            import math

            from .config import DEFAULT_GRID, SATELLITE_MODULES
            from .data.radar_render import geo_bounds
            from .data.satellite import ABISource, satellite_for
            from .data.volumes import bounded_window
            from .detection.cloudtop import Tracker as CloudTopTracker

            missing = missing_modules(SATELLITE_MODULES)
            if missing:
                self.satellite_window.set_busy(False)
                self.satellite_window.set_status(
                    f"Satellite needs the live extra ({', '.join(missing)} "
                    f"missing) — {LIVE_EXTRA_HINT}"
                )
                return

            w0, w1 = bounded_window(
                warning.issued, warning.expires,
                self.settings.pre_minutes, self.settings.post_minutes,
            )
            # Crop the satellite scene to the NEXRAD volume footprint (the radar
            # analysis-grid extent), so the full storm anvil is in frame rather
            # than just the warning polygon. Use the actual gridded volume bounds
            # when a download exists, else the site + grid half-width.
            site = self.resolver.for_polygon(warning.polygon)
            pad = self.settings.sat_bbox_pad_deg
            results = self._results.get(warning.id) or []
            if results:
                lon_min, lon_max, lat_min, lat_max = geo_bounds(results[0].volume)
                bbox = (lon_min - pad, lat_min - pad, lon_max + pad, lat_max + pad)
            else:
                half = DEFAULT_GRID.half_width_km
                dlat = half / 110.574
                dlon = half / (111.320 * max(0.1, math.cos(math.radians(site.lat))))
                bbox = (site.lon - dlon - pad, site.lat - dlat - pad,
                        site.lon + dlon + pad, site.lat + dlat + pad)
            _, label, short = satellite_for(warning.centroid[0], w0.date())
            cparams = self.settings.cloudtop

            self._sat_results = []
            self._sat_tracker = CloudTopTracker(cparams)  # provenance holder
            self._sat_persistence = None
            if self.dsn:
                from .persist import Persistence
                self._sat_persistence = Persistence(self.dsn)
                self._sat_persistence.connect()
                self._sat_persistence.upsert_warning(warning)
            if not self._results.get(warning.id):
                self.satellite_window.set_status(
                    f"{label}: {w0:%H:%MZ}–{w1:%H:%MZ} — no radar yet, tilt skipped."
                )
            else:
                self.satellite_window.set_status(f"{label}: {w0:%H:%MZ}–{w1:%H:%MZ}…")

            log.info("satellite.fetch", warning=warning.id, satellite=short,
                     start=w0.isoformat(), end=w1.isoformat())
            cancel = threading.Event()
            source = ABISource(
                w0, w1, bbox, satellite=short,
                target_res_deg=self.settings.sat_target_res_deg,
            )
            worker = SatelliteWorker(warning, source, cparams, cancel)
            worker.signals.status.connect(self.satellite_window.set_status)
            worker.signals.scene_ready.connect(self._on_scene)
            worker.signals.error.connect(
                lambda m: self.satellite_window.set_status("Error: " + m.splitlines()[0])
            )
            worker.signals.finished.connect(self._close_satellite_persistence)
            worker.signals.finished.connect(lambda: self.satellite_window.set_busy(False))
            self._workers.append(worker)
            self.pool.start(worker)

        def _close_satellite_persistence(self) -> None:
            if getattr(self, "_sat_persistence", None) is not None:
                self._sat_persistence.close()
                self._sat_persistence = None

        def _on_scene(self, warning, scene, cells, index, total) -> None:
            """GUI-thread radar association + render + persist for one ABI scene.

            The worker already detected and tracked the cloud-top cells; here we
            tilt them against the nearest radar volume (which lives on this
            thread) — which also annotates that volume's storms with cloud-top
            temperature — then cache, persist, refresh the listing, and overlay
            the scene on the map.
            """
            from .detection.cloudtop import associate_radar

            try:
                self.downloads.save_warning(warning)
                self.downloads.save_scene(warning.id, scene)
            except Exception as e:  # noqa: BLE001 - caching must never break a run
                log.error("gui.scene_cache_error", warning=warning.id, error=str(e))

            radar = self._results.get(warning.id, [])
            if radar:
                radar_res = min(
                    radar,
                    key=lambda r: abs(
                        (r.volume.valid_time - scene.valid_time).total_seconds()
                    ),
                )
                associate_radar(cells, radar_res.cells, self._sat_tracker.params)
                # The matched volume's storms now carry cloud-top temp —
                # refresh its row so the volume listing shows it, and rebuild
                # the track charts so the cloud-top series picks it up.
                if warning.id == self._selected_id:
                    self.volumes.add_result(radar_res)
                    self._refresh_tracks()

            if getattr(self, "_sat_persistence", None) is not None:
                try:
                    self._sat_persistence.upsert_cloudtops(
                        warning.id, warning.event, cells,
                        self._sat_tracker.params.settings_hash,
                    )
                except Exception as e:  # noqa: BLE001
                    log.error("gui.scene_persist_error", warning=warning.id, error=str(e))

            self._sat_results.append((scene, cells))
            if warning.id == self._selected_id:
                # Show the scene matched to the radar volume currently on the map
                # (not just the latest fetched), so the layers stay in sync.
                self._current_sat_time = None  # force a re-pick
                self._show_satellite_for()
            n_cells = sum(len(c) for _, c in self._sat_results)
            self.satellite_window.set_count(len(self._sat_results), n_cells)
            self.statusBar().showMessage(
                f"{warning.event}  satellite {scene.valid_time:%H%MZ}  "
                f"{index}/{total}  {len(cells)} cloud-top(s)"
            )

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
    window = _build_window(persist=False, use_local_settings=False)
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
    last_warning = None
    for warning in FixtureWarningSource(fixture):
        window._sources[warning.id] = (lambda d: (lambda: FixtureVolumeSource(d)))(fixture)
        window.search.add_result(warning)
        window.on_select(warning)
        site = resolver.for_polygon(warning.polygon)
        process_warning(
            warning, FixtureVolumeSource(fixture), site, window.params,
            on_result=window._on_volume,
        )
        last_warning = warning

    # Drive the satellite path offscreen too: a synthetic cloud-top scene
    # straight through _on_scene (radar association + map overlay + counts),
    # proving the wiring and the map JSON without any network.
    if last_warning is not None:
        from .data.synthetic import make_cold_scene
        from .detection.cloudtop import Tracker as CloudTopTracker, run as cloudtop_run
        window._reset_satellite()
        window._sat_tracker = CloudTopTracker(window.settings.cloudtop)
        lon0, lat0, lon1, lat1 = last_warning.polygon.bounds
        scene = make_cold_scene(
            "GOES-19", last_warning.issued,
            (lon0 - 0.3, lat0 - 0.3, lon1 + 0.3, lat1 + 0.3),
            0.5 * (lon0 + lon1), 0.5 * (lat0 + lat1),
        )
        cells = cloudtop_run(scene, window.settings.cloudtop)
        window._sat_tracker.update(cells, scene.valid_time)
        window._on_scene(last_warning, scene, cells, 1, 1)
        assert window._sat_results, "satellite scene did not register"

    # Drive the HRRR/vault path offscreen too: a synthetic freezing-level grid
    # straight through _on_hrrr_grid (vault detection + volume-listing refresh
    # + counts), proving the wiring without any network.
    if last_warning is not None:
        from .data.synthetic import make_freezing_grid
        window._reset_hrrr()
        lon0, lat0, lon1, lat1 = last_warning.polygon.bounds
        grid = make_freezing_grid(
            last_warning.issued,
            (lon0 - 1.5, lat0 - 1.5, lon1 + 1.5, lat1 + 1.5),
            level_m=3000.0,
        )
        window._on_hrrr_grid(last_warning, grid, 1, 1)
        assert window._hrrr_grids, "freezing-level grid did not register"
        assert any(
            c.freezing_level_km is not None
            for r in window._results.get(last_warning.id, []) for c in r.cells
        ), "vault detection did not annotate the cells"

    # Storm Track window: rebuild its track listing from the processed volumes
    # and drive a selection through to the map, proving the wiring end-to-end.
    if last_warning is not None:
        window.on_select(last_warning)  # arms + refreshes the track listing
        if window.stormtrack_window.model.rowCount():
            window.stormtrack_window.list.setCurrentIndex(
                window.stormtrack_window.model.index(0, 0)
            )
            assert window.stormtrack_window.charts is not None

    # Instantiate the modal dialogs to prove they build (8B).
    settings_dlg = SettingsDialog(window.dsn, window.settings_path, window)
    download_dlg = DownloadDialog("KFWS  Tornado Warning", total=9, parent=window)
    download_dlg.update_progress(3, 9, "KFWS 1142Z")
    assert window.search.results.count() >= 1
    assert settings_dlg.model.rowCount() >= 1

    app.processEvents()
    print(
        "smoke: panes (search, volumes, map, model, satellite, hrrr, "
        "storm-track) + settings/download dialogs instantiated, fixture + "
        "cloud-top scene rendered, freezing level + vault annotated, storm "
        "tracks built — OK"
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
