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

def _build_window(persist: bool):
    from PySide6.QtCore import Qt, QThreadPool
    from PySide6.QtWidgets import (
        QFileDialog,
        QInputDialog,
        QMainWindow,
        QSplitter,
        QStatusBar,
    )

    from .data.sites import SiteResolver
    from .data.volumes import FixtureVolumeSource, NexradArchiveSource, bounded_window
    from .data.warnings import FixtureWarningSource, IEMHistoricalSource
    from .panes.map_view import MapPane
    from .panes.model_view import ModelPane
    from .panes.nav import NavPane
    from .workers import WarningWorker

    class MainWindow(QMainWindow):
        def __init__(self) -> None:
            super().__init__()
            self.setWindowTitle("WxAlerts Storm Modeler — Phase A")
            self.resize(1500, 900)

            from .settings.resolver import resolve

            self.dsn = pg_dsn() if persist else None
            self.params = resolve(self.dsn).detection
            self.resolver = SiteResolver()
            self.pool = QThreadPool.globalInstance()
            self._workers: list = []

            self.nav = NavPane()
            self.map = MapPane()
            self.model = ModelPane()

            splitter = QSplitter(Qt.Horizontal)
            splitter.addWidget(self.nav)
            splitter.addWidget(self.map)
            splitter.addWidget(self.model)
            splitter.setStretchFactor(0, 0)
            splitter.setStretchFactor(1, 1)
            splitter.setStretchFactor(2, 0)
            # Nav defaults to ~15% width, draggable.
            splitter.setSizes([220, 940, 340])
            self.setCentralWidget(splitter)

            self.setStatusBar(QStatusBar())
            self.map.set_basemap()
            self._build_menu()

            self.nav.storm_selected.connect(self.map.highlight_cell)
            self.nav.storm_selected.connect(
                lambda c: self.statusBar().showMessage(
                    f"Storm id {c.cell_id}  track {c.track_id}  "
                    f"{c.max_dbz:.1f} dBZ  depth {c.depth_km:.1f} km"
                )
            )

        # --- menu ---------------------------------------------------------

        def _build_menu(self) -> None:
            run_menu = self.menuBar().addMenu("&Run")
            act_fix = run_menu.addAction("Replay fixture…")
            act_fix.triggered.connect(self._pick_fixture)
            act_range = run_menu.addAction("Run date range (IEM)…")
            act_range.triggered.connect(self._pick_date_range)
            run_menu.addSeparator()
            act_stop = run_menu.addAction("Stop")
            act_stop.triggered.connect(self.stop)

        def _pick_fixture(self) -> None:
            d = QFileDialog.getExistingDirectory(
                self, "Choose replay fixture", str(FIXTURE_DIR)
            )
            if d:
                self.start_replay(d)

        def _pick_date_range(self) -> None:
            text, ok = QInputDialog.getText(
                self,
                "IEM date range",
                "UTC range  start,end  (e.g. 2024-05-25T17:00Z,2024-05-25T19:00Z):",
            )
            if ok and "," in text:
                s, e = (t.strip() for t in text.split(",", 1))
                self.start_live(s, e)

        # --- runs ---------------------------------------------------------

        def _submit(self, warning, volume_source, site) -> None:
            worker = WarningWorker(
                warning, volume_source, site, self.params, dsn=self.dsn
            )
            worker.signals.warning_started.connect(self.map.show_warning)
            worker.signals.volume_done.connect(self._on_volume)
            worker.signals.error.connect(self._on_error)
            self._workers.append(worker)
            self.pool.start(worker)

        def start_replay(self, fixture_dir) -> None:
            self.statusBar().showMessage(f"Replaying {fixture_dir}…")
            for warning in FixtureWarningSource(fixture_dir):
                site = self.resolver.for_polygon(warning.polygon)
                self._submit(warning, FixtureVolumeSource(fixture_dir), site)

        def start_live(self, start_iso: str, end_iso: str) -> None:
            def _p(s: str) -> datetime:
                return datetime.fromisoformat(
                    s.replace("Z", "+00:00")
                ).astimezone(timezone.utc)

            start, end = _p(start_iso), _p(end_iso)
            self.statusBar().showMessage(f"Querying IEM {start_iso}…{end_iso}")
            for warning in IEMHistoricalSource(start, end):
                site = self.resolver.for_polygon(warning.polygon)
                w0, w1 = bounded_window(warning.issued, warning.expires)
                vs = NexradArchiveSource(site.icao, w0, w1, site.lat, site.lon)
                self._submit(warning, vs, site)

        def stop(self) -> None:
            self.pool.clear()
            self.statusBar().showMessage("Stopped (queued work cleared).")

        # --- signals ------------------------------------------------------

        def _on_volume(self, res) -> None:
            self.nav.add_result(res)
            self.map.show_result(res.warning, res.volume, res.cells)
            self.statusBar().showMessage(
                f"{res.warning.event}  {res.volume.valid_time:%H%MZ}  "
                f"{len(res.cells)} cell(s)"
            )

        def _on_error(self, msg: str) -> None:
            log.error("worker.error", msg=msg)
            self.statusBar().showMessage("Error: " + msg.splitlines()[0])

    return MainWindow()


def run_gui(args: argparse.Namespace) -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    window = _build_window(persist=args.persist)
    window.show()
    if args.replay:
        window.start_replay(args.replay)
    elif args.from_ and args.to:
        window.start_live(args.from_, args.to)
    return app.exec()


def _ensure_display():
    """Guarantee a working GL context for the offscreen smoke.

    Some headless stacks have flaky offscreen (EGL/OSMesa) GL where VTK's
    shader compilation intermittently aborts. If no display is set, start a
    private Xvfb (software llvmpipe) and point both VTK *and* Qt (xcb) at it, so
    the documented ``QT_QPA_PLATFORM=offscreen --smoke`` command renders against
    a real, stable GL context and exits deterministically. Returns the Xvfb
    process handle (or ``None``) so the caller can reap it.
    """
    if os.environ.get("DISPLAY"):
        return None
    import shutil
    import subprocess
    import time

    if not shutil.which("Xvfb"):
        return None  # no Xvfb — fall back to offscreen GL (best effort)
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

    # Drive one fixture synchronously through the panes (no threads) so the
    # smoke run is deterministic and self-contained.
    from .data.sites import SiteResolver
    from .data.volumes import FixtureVolumeSource
    from .data.warnings import FixtureWarningSource
    from .pipeline import process_warning

    fixture = Path(args.replay) if args.replay else FIXTURE_DIR / "tornado_warning_case"
    resolver = SiteResolver()
    for warning in FixtureWarningSource(fixture):
        site = resolver.for_polygon(warning.polygon)
        window.map.show_warning(warning)
        process_warning(
            warning,
            FixtureVolumeSource(fixture),
            site,
            window.params,
            on_result=window._on_volume,
        )

    app.processEvents()
    print("smoke: panes instantiated, fixture rendered — OK")
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
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.smoke:
        return run_smoke(args)
    if args.headless:
        return run_headless(args)
    return run_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
