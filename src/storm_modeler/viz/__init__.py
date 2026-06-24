"""Phase B visualisation layer.

Pure (Qt-free) building blocks for the 3D model pane:

* :mod:`grid_provider` — on-demand Cartesian grids for a warning's volumes,
  served from an LRU cache so scrubbing is responsive.
* :mod:`scene_builder` — grid + cell → VTK/PyVista actors (volume render,
  isosurface shells, envelope prism, height marker).
* :mod:`xsection` — vertical cross-section extraction + 2D render (Phase B2).
"""

from __future__ import annotations

from .grid_provider import GridProvider

__all__ = ["GridProvider"]
