"""Offscreen GUI wiring tests.

Builds the real panes offscreen, pushes one fixture through, and asserts the
nav tree populates and the map accepts a storm selection (recenter/highlight)
without raising. Forces the Qt offscreen platform so this runs with no display.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyvista as pv  # noqa: E402

pv.OFF_SCREEN = True

import pytest  # noqa: E402

from storm_modeler.config import FIXTURE_DIR  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _drive_tornado(window):
    from storm_modeler.data.sites import SiteResolver
    from storm_modeler.data.volumes import FixtureVolumeSource
    from storm_modeler.data.warnings import FixtureWarningSource
    from storm_modeler.pipeline import process_warning

    fixture = FIXTURE_DIR / "tornado_warning_case"
    resolver = SiteResolver()
    cells_seen = []
    for warning in FixtureWarningSource(fixture):
        site = resolver.for_polygon(warning.polygon)
        window.map.show_warning(warning)

        def on_res(res):
            window._on_volume(res)
            cells_seen.extend(res.cells)

        process_warning(
            warning, FixtureVolumeSource(fixture), site, window.config, on_result=on_res
        )
    return cells_seen


def test_panes_build_and_populate(qapp):
    from storm_modeler.app import _build_window

    window = _build_window(persist=False)
    assert window.nav is not None and window.map is not None and window.model is not None

    cells = _drive_tornado(window)
    assert cells, "expected at least one tracked storm cell"

    # Nav tree populated: at least one State row, and a Storm leaf carrying a cell.
    assert window.nav.model.rowCount() >= 1
    assert window.map._radar_actor is not None  # self-rendered radar layer added

    # Selecting a storm recenters + highlights without raising.
    window.map.highlight_cell(cells[0])
    assert window.map._highlight_actor is not None

    # Finalise the render window so interpreter teardown stays quiet.
    window.map.plotter.close()
