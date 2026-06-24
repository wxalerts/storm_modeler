"""Headless off-screen render of a fixture cell → PNG (Section 8 validation).

Runs the deterministic fixture pipeline, picks a cell, and renders either the 3D
perspective scene (``--view 3d``, B1) or the vertical cross-section
(``--view xsection``, B2) to a PNG with PyVista's off-screen GL. Used by the 8A
/ 8B asserts that the scene is non-empty.

    uv run python -m storm_modeler.tools.render_cell \\
        --fixture tornado_warning_case --cell first --view 3d --out /tmp/cell3d.png
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config import FIXTURE_DIR, pg_dsn
from ..data.sites import SiteResolver
from ..data.volumes import FixtureVolumeSource
from ..data.warnings import FixtureWarningSource
from ..detection.detection_v2 import StormCell
from ..pipeline import VolumeResult, process_warning
from ..settings.resolver import resolve


def _fixture_dir(name: str) -> Path:
    p = Path(name)
    if p.exists():
        return p
    return FIXTURE_DIR / name


def _run_pipeline(fixture: Path):
    settings = resolve(pg_dsn())
    warning = next(iter(FixtureWarningSource(fixture)))
    site = SiteResolver().for_polygon(warning.polygon)
    results = process_warning(
        warning, FixtureVolumeSource(fixture), site, settings.detection
    )
    return warning, results, settings


def _select(results: list[VolumeResult], selector: str):
    """Return (VolumeResult, StormCell) for ``selector`` (``first`` or an int)."""
    nonempty = [r for r in results if r.cells]
    if not nonempty:
        raise SystemExit("no cells detected in the fixture")
    if selector == "first":
        res = nonempty[0]
        return res, res.cells[0]
    # Numeric: index of the strongest-cell volume, else a cell_id lookup.
    idx = int(selector)
    res = nonempty[min(idx, len(nonempty) - 1)]
    return res, res.cells[0]


def _new_plotter(off_screen: bool = True):
    import pyvista as pv

    plotter = pv.Plotter(off_screen=off_screen, window_size=(1024, 768))
    plotter.set_background("black")
    return plotter


def render_3d(res: VolumeResult, cell: StormCell, viewer, out: Path) -> None:
    from ..viz import scene_builder

    plotter = _new_plotter()
    scene_builder.build_scene(
        plotter, res.volume, cell, viewer, other_cells=res.cells,
    )
    plotter.show_bounds(
        xtitle="x (km E)", ytitle="y (km N)",
        ztitle=f"z (km x{viewer.vert_exag:g})", color="gray",
    )
    plotter.view_isometric()
    plotter.camera.zoom(1.4)
    plotter.screenshot(str(out))
    plotter.close()


def render_xsection(
    warning, res: VolumeResult, cell: StormCell, results, viewer, out: Path
) -> None:
    from ..viz import xsection

    xsection.render_section(
        res.volume, cell, results, viewer, str(out), off_screen=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="render_cell", description=__doc__)
    ap.add_argument("--fixture", required=True, help="fixture name or directory")
    ap.add_argument("--cell", default="first", help="'first' or an index")
    ap.add_argument("--view", choices=("3d", "xsection"), default="3d")
    ap.add_argument("--out", required=True, help="output PNG path")
    args = ap.parse_args(argv)

    # A real GL context (Xvfb if headless) makes off-screen VTK deterministic.
    from ..app import _ensure_display

    xvfb = _ensure_display()
    try:
        fixture = _fixture_dir(args.fixture)
        warning, results, settings = _run_pipeline(fixture)
        res, cell = _select(results, args.cell)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        if args.view == "3d":
            render_3d(res, cell, settings.viewer, out)
        else:
            render_xsection(warning, res, cell, results, settings.viewer, out)
        print(f"rendered {args.view} -> {out}")
    finally:
        if xvfb is not None:
            xvfb.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
