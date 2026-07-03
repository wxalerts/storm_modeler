# SRM implementation plan

Storm-Relative Motion (SRM) product + AWIPS velocity palette. Phased; one
commit per phase; no pushes.

## Phase 0 findings (contradictions with the task's Context section)

1. **Velocity does not render `RdYlGn_r` anymore.** The working tree already
   carried an (uncommitted) AWIPS `Vel.cmap` implementation from a prior
   session: a hex-embedded 256-entry table in `radar_render.py`
   (`NWS_VEL_TABLE`, `vel_to_rgba`) plus range-folded extraction in
   `volumes.py`. Phase 1 therefore *reshapes* that work into the form this
   task specifies (checked-in `.npz` asset + reproducible parser script)
   rather than starting from matplotlib. The unrelated in-tree HRRR/vault
   feature was committed separately first (`b4fc56b`) so phase commits stay
   clean.
2. **The RF layer exists, named `range_folded`** (not `range_folded_mask`):
   `GriddedVolume.products["range_folded"]` is a 0/1 float32 lowest-observed-
   tilt layer written by `data/volumes.py::grid_level2` (raw Level II value 1
   recovered by `_velocity_range_folded`; Py-ART itself masks RF away).
   Volumes cached before that change lack the layer — RF purple applies only
   when present, which is the graceful-skip the task asks for.
3. **`src/scit/` does not exist.** There is no `detect_velocity_couplets`, no
   `tgen.py`, and no couplet code anywhere in the repo (verified by grep).
   The repo's "SCIT" is `storm_modeler/detection/detection_v2`, which is
   reflectivity-based storm-cell identification only. Phase 4 will implement
   `correct_vrot` as a pure, unwired function in
   `src/storm_modeler/detection/srm.py`; the "optional `storm_motion`
   parameter on `detect_velocity_couplets`/tgen" cannot be done without
   inventing couplet detection, which the task forbids — documented as a
   deviation in `REPORT_SRM.md`.
4. **No repo-root `tools/`.** Repo convention is the `storm_modeler.tools`
   package (`render_cell.py`, `set_setting.py`, run via `python -m`). The LUT
   parser goes there: `src/storm_modeler/tools/build_awips_vel_lut.py`.

Confirmed as described: `GriddedVolume.products` 2D layers and where
`velocity` is written (`grid_level2`); radial unit vector `(x, y)/r` (radar at
grid origin); the range-ring meshgrid pattern in `radar_render.py`; map moment
label built by `map_client._product_label` and sent as the `product` field of
the `radar` command to the GTK/Leaflet process; Product menu and P-key cycle
built from the `PRODUCTS` list order in `app._build_product_menu` /
`cycle_product` (so SRM slots in by list position); selection flow
`volumes.storm_selected → app._on_storm(cell)` with per-frame scrub updates
via `model.frame_ready → app._on_model_frame(volume, cell, cells)`;
`viz/xsection.py::section_azimuth` "track" branch walks `_track_seeds` and
takes the segment bracketing the cell's valid_time (>1 m displacement gate).

## Phase 1 — AWIPS velocity palette (asset-based)

- `src/storm_modeler/tools/build_awips_vel_lut.py` (new): fetch/parse the
  AWIPS `8-bit Vel.cmap` XML → write `src/storm_modeler/assets/awips_vel_lut.npz`
  (`rgba_float`, 256×4, [0,1]). Checked-in asset; raw XML not vendored.
- `src/storm_modeler/data/radar_render.py`: replace the hex blob with a
  module-level LUT load; `vel_to_rgba` maps m/s → indices 2–255 over
  vmin/vmax −32/+32, NaN → 0, RF → 1 before the alpha pass.
- `src/storm_modeler/tests/test_radar_render_vel_lut.py` (new): LUT shape,
  index-0 alpha, NaN transparency, gray center at 0 m/s, endpoint indices,
  clipping. Existing `tests/test_radar_render.py` updated to the LUT names.

## Phase 2 — SRM derivation + product registration

- `radar_render.py`: `derive_srm(volume, u_ms, v_ms)` (radial-projection
  subtraction, r floored at 1 m, NaN-preserving); `ensure_srm_layer(volume,
  u_ms, v_ms)` stashing `products["storm_relative_velocity"]`, motion memo in
  a module-side `WeakKeyDictionary`, recompute only on >0.1 m/s change;
  PRODUCTS entry `storm_relative_velocity` right after velocity, sharing the
  LUT window; `_colorize` treats SRM like velocity (incl. RF purple).
- `src/storm_modeler/tests/test_srm.py` (new): uniform-flow cancellation,
  zero motion identity, NaN preservation, translated Rankine couplet.

## Phase 3 — Map plumbing + app wiring

- `src/storm_modeler/viz/motion.py` (new): `_track_seeds` moves here;
  `track_segment(cell, results)` returns the bracketing displacement
  `(dx_m, dy_m, dt_s) | None`; `track_motion_uv(...)` returns `(u, v) | None`;
  meteorological speed/dir ⇄ components converters (dir = FROM convention).
- `viz/xsection.py`: `section_azimuth` "track" branch delegates to the shared
  segment walk — behavior identical, `test_xsection.py` unchanged.
- `panes/map_client.py`: `set_storm_motion(u, v)`; `set_product`/`set_radar`
  call `ensure_srm_layer` for SRM; SRM moment label
  `SRM · {speed_kt:.0f} kt @ {dir_deg:03.0f}°`.
- `app.py`: motion resolution (selected track → mean of live tracks → manual
  setting → (0,0)), `srm.motion_fallback` structlog warning, pushed on storm
  selection and every scrub frame.
- `settings/registry.py`: `srm_motion_source` (choice), `srm_manual_speed_kt`,
  `srm_manual_dir_deg` in the Display group; NOT in `DETECTION_KEYS`.
- `tests/test_motion.py` (new): segment walk, single-seed None, dir/speed
  round-trip, fallback order (pure function).

## Phase 4 — Vrot correction helper (pure, unwired)

- `src/storm_modeler/detection/srm.py` (new): `correct_vrot(v_max, v_min,
  center_az_deg, u_ms, v_ms)` → `(vrot_ms, v_max_sr, v_min_sr)`; two-signed /
  one-sided rule applied after the scalar storm-motion shift.
- `tests/test_vrot.py` (new): ±25 couplet regression through +30 m/s flow;
  az=0°/90° projection axes. No detection wiring (see finding 3).

## Phase 5 — Verification + report

- Full `uv run pytest`; headless velocity + SRM frame render from a fixture
  volume (synthetic velocity layer — shipped fixtures carry no velocity);
  `REPORT_SRM.md` with per-file changes, test counts, and deviations.
