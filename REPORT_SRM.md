# SRM implementation report

Storm-Relative Motion product + AWIPS velocity palette. Full suite: **106
passed, 1 skipped** (the skip is pre-existing, unrelated). Commits, in order:

| Phase | Commit | Message |
|---|---|---|
| pre-work | `b4fc56b` | `feat(hrrr): freezing-level pane + vault-derived OT; drop unreliable GOES OT flag` |
| 0 | `64145b6` | `docs: SRM implementation plan` |
| 1 | `6f61a0e` | `feat(render): AWIPS 8-bit velocity palette with RF purple` |
| 2 | `b10014e` | `feat(render): storm-relative velocity product` |
| 3 | `f92ce78` | `feat(app): SRM product cycling, motion plumbing, settings knobs` |
| 4 | `17e9309` | `feat(scit): storm-relative Vrot correction helper (unwired)` |
| 5 | (this commit) | `test: SRM verification + report` |

Nothing pushed. No detection thresholds, admission logic, `detection_v2`
internals, tracking logic, or cache formats were modified. New code is
numpy + stdlib only.

## Phase 0 discoveries that contradict the task's Context

1. **Velocity was no longer `RdYlGn_r`.** The working tree already carried an
   uncommitted AWIPS `Vel.cmap` implementation (hex-embedded table) plus
   range-folded extraction from a prior session, entangled with an unrelated
   uncommitted HRRR/vault feature. The HRRR feature was committed first on its
   own (`b4fc56b`) so the phase commits stay single-topic; Phase 1 then
   *reshaped* the palette work into the form this task specifies (checked-in
   `.npz` asset + reproducible parser) instead of starting from matplotlib.
2. **The RF layer is named `range_folded`, not `range_folded_mask`** —
   `GriddedVolume.products["range_folded"]`, a 0/1 float32 layer built in
   `grid_level2` by re-decoding the raw Level II VEL bytes (Py-ART masks
   `raw <= 1`, conflating "range folded" (1) with "below threshold" (0)).
   Volumes cached before that layer existed simply lack it; RF purple then
   skips gracefully, as required.
3. **`src/scit/` does not exist.** No `detect.py`, no
   `detect_velocity_couplets`, no `tgen.py`, no couplet dicts, no `vr_ms`
   key — verified by repo-wide grep. The repo's "SCIT" is
   `storm_modeler/detection/detection_v2` and identifies reflectivity cells
   only. Consequences for Phase 4 are under Deviations.
4. **No repo-root `tools/`.** The repo's tool convention is the
   `storm_modeler.tools` package (`render_cell`, `set_setting`); the LUT
   parser lives there instead.

## What changed, per file

### Phase 1 — AWIPS palette (`6f61a0e`)

- `src/storm_modeler/tools/build_awips_vel_lut.py` (new): fetches/parses the
  AWIPS `8-bit Vel.cmap` XML (Unidata awips2, pinned tag `unidata_17.1.1`),
  validates the index semantics (0 transparent, 1 = #7F00CF), writes the
  asset. Raw XML not vendored.
- `src/storm_modeler/assets/awips_vel_lut.npz` (new, checked in):
  `rgba_float` (256×4) in [0, 1].
- `src/storm_modeler/data/radar_render.py`: hex blob replaced by a
  module-level asset load (`AWIPS_VEL_LUT`, uint8 views `_VEL_LUT_U8` /
  `NWS_VEL_TABLE`); `vel_to_rgba` maps m/s → indices 2–255 over the fixed
  −32…+32 m/s window (clip, not wrap), NaN → 0, RF mask → 1 before the alpha
  pass (alpha now comes from the LUT's alpha column); `_colorize` accepts the
  RF mask and `product_lonlat_image` passes the resampled `range_folded`
  layer through it (previously overpainted after colorize — same pixels,
  spec'd structure).
- `src/storm_modeler/data/volumes.py`: buffers the archive bytes once —
  `read_nexrad_archive` closes the handle it is given, which broke the
  second (RF-extraction) pass over the same stream.
- Tests: `test_radar_render_vel_lut.py` (new, 7 tests: asset shape, index-0
  alpha, all-NaN transparency, gray center at 0 m/s, endpoint indices,
  clipping, RF purple); `test_radar_render.py` kept passing unchanged.

### Phase 2 — SRM product (`b10014e`)

- `radar_render.py`: `derive_srm(volume, u_ms, v_ms)` — base velocity minus
  `(u·x + v·y)/r` with `r` floored at 1 m (radar at grid origin ⇒ no azimuth
  trig), NaN-preserving, dtype-preserving (float32 cast happens only at
  display storage so the atol-1e-6 identity tests are honest);
  `ensure_srm_layer` injects `products["storm_relative_velocity"]` and
  records the motion used as a **non-field attribute** on the volume
  (`_srm_motion_uv`) — `save_npz` serialises declared fields + products only,
  so the on-disk cache format is untouched; recompute only when either
  component moves > 0.1 m/s. PRODUCTS gains `storm_relative_velocity`
  ("SRM", m/s) immediately after velocity, sharing the LUT, window, and RF
  purple.
- Tests: `test_srm.py` (6): uniform-flow cancellation (atol 1e-6), zero-motion
  identity, NaN preservation, translated-Rankine recovery (atol 1e-6, and the
  ground-relative field is verifiably one-sided while SRM restores ±25),
  cache-until-motion-changes, graceful None without a velocity layer.

### Phase 3 — plumbing + wiring (`f92ce78`)

- `src/storm_modeler/viz/motion.py` (new): `track_seeds`/`track_segment`
  factored from `xsection.section_azimuth`'s track branch (identical segment
  choice and >1 m displacement gate); `track_motion_uv`, `mean_motion_uv`;
  meteorological conversions `uv_from_speed_dir`/`speed_dir_from_uv`
  (dir = bearing the storm moves FROM, i.e. 240° @ 30 kt moves northeast —
  matching how NWS storm motion is read; the task's "direction of motion,
  meteorological bearing" was ambiguous between to/from, FROM chosen and
  documented); `resolve_motion` — the pure fallback chain manual → selected
  track → mean of live tracks → (0, 0).
- `viz/xsection.py`: track branch delegates to `track_segment`;
  `test_xsection.py` passes unchanged.
- `panes/map_client.py`: `set_storm_motion(u, v)` (re-renders when SRM is
  showing and the motion moved > 0.1 m/s); `set_radar` re-derives the SRM
  layer per volume before the missing-product check, so scrubbing onto a new
  volume keeps the frame; the SRM moment label is always
  `SRM · {speed_kt:.0f} kt @ {dir_deg:03.0f}°`.
- `app.py`: `_update_srm_motion` resolves per `srm_motion_source` and pushes
  to the map on storm selection (`_on_storm`), Storm-Track-window selection
  (`_on_track_selected`, anchored to the track's latest cell), every scrub
  frame (`_on_model_frame`, before the map re-render), and settings reload;
  fallback drops log `srm.motion_fallback` (structlog, warning). Product menu
  + P-cycle pick SRM up automatically from its PRODUCTS position.
- `settings/registry.py`: `srm_motion_source`
  (`selected_track|mean_tracks|manual`), `srm_manual_speed_kt` (0–120,
  default 30), `srm_manual_dir_deg` (0–360, default 240) in the Display
  group. Not in `DETECTION_KEYS` (or any provenance-hash key set).
- Tests: `test_motion.py` (8): east-moving seeds → (u>0, v≈0), single-seed /
  untracked → None, mean over live tracks, FROM-convention round-trip,
  resolver order incl. mean-source skipping the selected track.

### Phase 4 — Vrot helper (`17e9309`)

- `src/storm_modeler/detection/srm.py` (new): `correct_vrot(v_max, v_min,
  center_az_deg, u_ms, v_ms) → (vrot_ms, v_max_sr, v_min_sr)` — scalar
  storm-motion projection at the couplet's center azimuth shifts both
  extrema, then the standard two-signed / one-sided rule. Imported by
  nothing, deliberately.
- Tests: `test_vrot.py` (5): ±25 two-signed baseline; the regression (−55/−5
  one-sided under 30 m/s flow degrades through the weak-signal branch,
  recovers exactly 25 with the matching motion); az 0° uses only `v_ms`,
  az 90° only `u_ms`; projection identity at an arbitrary azimuth.

## Deviations from the prompt, with justification

1. **Phase 4 wiring skipped** (`detect_velocity_couplets` / tgen optional
   `storm_motion` parameter, `vr_sr_ms` key): those functions/files do not
   exist anywhere in the repo, and creating them would mean inventing couplet
   detection — explicitly forbidden ("do not build or simulate any
   couplet→cell association logic"; "the pass does not exist and must not be
   invented here"). Only the pure helper + its regression tests ship. It
   lives in `storm_modeler/detection/srm.py` rather than `src/scit/` because
   a new top-level `scit` package would also require packaging changes
   (hatch wheel config packages `src/storm_modeler` only) for code nothing
   imports.
2. **LUT parser location**: `src/storm_modeler/tools/` (the repo's existing
   runnable-tools package) instead of a new repo-root `tools/`.
3. **RF layer name**: the pre-existing `range_folded` products layer is used
   as the "range_folded_mask" — same semantics (0/1, velocity-family gates),
   established name.
4. **Phase 5 render**: `render_cell` renders the 3D/cross-section
   reflectivity views, not map products, and the shipped fixtures carry no
   velocity layer — so verification rendered the map products directly via
   `product_lonlat_image` on a real cached KDVN Level II volume
   (2026-07-02 22:33 Z, velocity + range_folded layers) instead: velocity and
   SRM (30 kt @ 240°) frames both fully populated (380,280 opaque pixels
   each), non-identical (374,046 pixels differ), RF purple present in both,
   and the SRM zero-isodop visibly rotated onto the storm-motion normal —
   the expected frame shift.
5. **Pre-work commit** (`b4fc56b`): unrelated, fully-tested HRRR/vault work
   was sitting uncommitted in the tree (see Phase 0 finding 1); it was
   committed as its own feature first so every phase commit is single-topic.
   Nothing was reverted or discarded.

## Test counts

- Before this task's phases: 89 tests (80 + 9 pre-existing-WIP velocity tests
  committed with Phase 1's reshape).
- After: **106 passed, 1 skipped** — 26 new tests across
  `test_radar_render_vel_lut.py` (7), `test_srm.py` (6), `test_motion.py`
  (8), `test_vrot.py` (5), with `test_xsection.py` and the rest of the
  pre-existing suite passing unchanged.
