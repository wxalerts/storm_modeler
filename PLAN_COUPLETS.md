# Couplet detection + rotation markers — plan

Phased; one commit per phase; no pushes. Builds on the merged SRM work
(`detection/srm.py::correct_vrot` — wired for the first time here,
`viz/motion.py` motion helpers).

## Phase 0 findings

- `GriddedVolume.products["velocity"]` confirmed: (ny, nx) float32 m/s, NaN
  where no data, written by `grid_level2`. **Caveat vs the prompt's "lowest
  tilt" wording:** the layer is the *lowest observed grid level per column*
  of the Barnes-gridded volume, not a true single-tilt PPI. Detection works
  the same; grid-scale shear thresholds are the tunables for exactly this
  reason.
- **No elevation angle is recoverable** — `GriddedVolume` stores only the
  Cartesian z axis; no per-tilt metadata survives gridding. Markers therefore
  omit beam height (per the prompt: not invented).
- Map marker pattern: lightning = compact point arrays onto an `L.canvas`
  renderer (`circleMarker`); cloud tops = GeoJSON FeatureCollection with
  numeric properties, popup text built JS-side. Couplets follow the GeoJSON
  pattern (`set_couplets`/`clear_couplets` symmetrical with
  `show_lightning`/`clear_lightning`). **Deviation:** inverted triangles
  cannot be `circleMarker`s; with at most a handful of couplets per volume
  they render as fixed-size SVG `divIcon` markers instead of canvas (canvas
  triangles would need a custom Leaflet marker class for no benefit at this
  count).
- Data pane: `LeftVolumesPane.add_result` wipes and re-adds each volume
  node's child rows (idempotent refresh) — couplet rows slot in after the
  Storm rows; a `COUPLET_ROLE` + `couplet_selected` signal mirrors the
  existing `CELL_ROLE` click path, wired to a cheap map pan (the GTK map
  already has a `pan` command).
- `detect_couplets(volume, cells, params)` as specified cannot reach track
  *history* (motion needs the cross-volume results list) — it gains an
  optional trailing `results=None` argument; `None` degrades the motion chain
  to `"none"`.
- LOT ETN 0002 cache present: 26 KLOT volumes 20:48–22:52 Z, all carrying the
  velocity layer → Phase 4 runs the real-case table.
- Sign derivation (documented in the module docstring, locked by test): with
  t̂ = (−y, x)/r (counterclockwise), a NH cyclonic (CCW) vortex gives
  **negative** azimuthal shear — at a vortex due north, outbound is east of
  center (∂v/∂x > 0) while t̂ points west, so t̂·∇v = −∂v/∂x < 0.

## Phase 1 — `detection/couplets.py`

NaN-aware central differences (slicing; NaN propagates through any stencil
touching NaN), shear = t̂·∇v masked where v is NaN or r < 5 km or
r > `max_range_km`; |shear| threshold → 8-connected `ndimage.label` → area
gate → |shear|-weighted centroid → `xy_to_lonlat`; extrema from the component
dilated 2 cells; motion = nearest tracked cell seed within `assoc_max_km`
(centroid distance; TODO envelope distance) → volume mean → (0, 0);
`vr_ms`/`vr_sr_ms` via `correct_vrot`. `CoupletParams` (defaults only) lands
in `settings/resolver.py` now; registry wiring is Phase 3. Tests per prompt
(`test_couplets.py`), reusing `test_srm.py`'s Rankine construction; the
frame-invariance case translates the vortex *along* the radial (vortex due
north + northward motion) so `vr_ms` genuinely degrades one-sided.

## Phase 2 — map + Data pane

- `map_client.set_couplets(couplets, min_vrot_kt)` / `clear_couplets()`:
  GeoJSON points with band (weak/moderate/strong from `vr_sr_ms` in kt),
  cyclonic flag, popup numbers; sub-threshold couplets not sent.
- `map_window_gtk.py`: `setCouplets`/`clearCouplets` + dispatch; inverted
  SVG-triangle divIcons — hollow (weak) / filled (moderate) / filled+larger
  (strong); red/white family cyclonic, cyan/blue anticyclonic; popup first
  line CYCLONIC/ANTICYCLONIC ROTATION. No "tornado" anywhere.
- `pipeline.VolumeResult` gains `couplets: list = []` (container field only —
  SCIT logic untouched); app computes couplets in `_on_gridded` and
  `_reprocess_current` (the cells' lifecycle hooks) and pushes
  `set_couplets` wherever the volume is (re)shown (`_on_volume`,
  `_on_model_frame`); `left_volumes` adds couplet child rows + pan-on-click.

## Phase 3 — settings

`GROUP_ROTATION` with the five `couplet_*` keys (defaults 0.004 s⁻¹, 4 km²,
150 km, 10 km, 15 kt), `COUPLET_KEYS`, `CoupletParams.from_dict`, resolver
`couplets` property + `couplet_marker_min_vrot_kt` accessor. Not in
`DETECTION_KEYS`; provenance note left as a comment.

## Phase 4 — verification + report

Full pytest; headless run over the cached LOT case (SCIT + tracker per volume
for motion, then `detect_couplets`) → per-volume table in
`REPORT_COUPLETS.md`; contradictions summarized there.
