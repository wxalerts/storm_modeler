# Couplet detection + rotation markers — report

Full suite: **112 passed, 1 skipped** (skip pre-existing). 6 new tests in
`test_couplets.py`; every pre-existing test untouched and green. Commits:
`6f24ab7` (plan) → `d44e251` (detection) → `6bde3e3` (map + pane) →
`94392db` (settings) → this commit. Nothing pushed.

## Phase 0 findings vs the prompt

1. **"Lowest tilt" is approximate.** `products["velocity"]` is the lowest
   *observed grid level per column* of the Barnes-gridded volume, not a true
   single-tilt PPI. Detection works identically; the shear threshold is a
   grid-scale knob for exactly this reason.
2. **No elevation angle survives gridding** — markers/popups carry no beam
   height, as instructed (nothing invented).
3. **`detect_couplets(volume, cells, params)` can't reach track history** —
   the motion association needs the warning's results list, so the function
   gained an optional trailing `results=None` argument. Without it the motion
   chain reports `"none"`.
4. **Triangles are SVG divIcons, not canvas.** Lightning uses `circleMarker`
   on an `L.canvas` renderer; a triangle needs a custom canvas marker class,
   which buys nothing at a handful of markers per volume. Fixed-pixel SVG
   `divIcon`s keep the styling contract (hollow/filled/larger, red vs cyan).
5. **Frame invariance is asymptotic, not exact.** A uniform flow's own radial
   projection carries azimuthal shear `|U|·sin(Δθ)/r` — ~6×10⁻⁴ s⁻¹ at 50 km
   for 30 m/s, harmless, but *above* the 0.004 threshold inside ~7.5 km. The
   built-in 5 km near gate plus the synthetic test's 15 km data blank cover
   it; the invariance test asserts couplet-level identity (count, centroid,
   area ±2 km²) rather than bit-identical masks.
6. **Sign convention (derived, tested):** with the counterclockwise
   `t̂ = (-y, x)/r`, a NH cyclonic vortex yields **negative** azimuthal shear
   (derivation in the `detection/couplets.py` docstring).
7. LOT ETN 0002 cache present → real-case run below.

## LOT ETN 0002 (KLOT, 2026-03-10, 26 volumes 20:48–22:58 Z)

Headless: SCIT + tracking per volume (for motion association), then
`detect_couplets`. **For human review — no coordinates asserted.**

### Registry defaults (min_shear 0.004 s⁻¹, min_area 4 km²)

Total 1,815 couplets across 26 volumes (40–112 per volume). The default
threshold is clearly **permissive on this squall-line case** — it captures
line-scale shear texture, not discrete mesocyclones — which is the expected
starting point per the settings note ("tuned in-app against the 2026-03-10
LOT case"). Strongest-per-volume, defaults:

| time | n | strongest vr_sr | sign | centroid (lat, lon) | range | motion |
|---|---|---|---|---|---|---|
| 20:48Z | 40 | 28.5 m/s (55 kt) | cyc | 41.188, -88.432 | 55 km | track:2 |
| 20:53Z | 37 | 27.8 m/s (54 kt) | anti | 41.374, -88.402 | 37 km | volume_mean |
| 20:58Z | 41 | 26.8 m/s (52 kt) | cyc | 41.336, -88.335 | 36 km | volume_mean |
| 21:03Z | 35 | 30.5 m/s (59 kt) | cyc | 40.888, -88.806 | 100 km | volume_mean |
| 21:07Z | 40 | 32.2 m/s (63 kt) | anti | 41.768, -87.719 | 35 km | volume_mean |
| 21:12Z | 48 | 32.5 m/s (63 kt) | cyc | 40.966, -88.717 | 88 km | volume_mean |
| 21:16Z | 49 | 29.8 m/s (58 kt) | cyc | 41.772, -87.600 | 44 km | volume_mean |
| 21:21Z | 51 | 30.2 m/s (59 kt) | cyc | 41.519, -88.462 | 33 km | volume_mean |
| 21:27Z | 59 | 32.6 m/s (63 kt) | cyc | 41.181, -88.243 | 49 km | volume_mean |
| 21:32Z | 59 | 33.0 m/s (64 kt) | cyc | 40.839, -88.696 | 99 km | volume_mean |
| 21:37Z | 57 | 32.2 m/s (63 kt) | anti | 41.551, -87.759 | 28 km | volume_mean |
| 21:42Z | 64 | 32.8 m/s (64 kt) | cyc | 41.577, -87.731 | 30 km | volume_mean |
| 21:48Z | 68 | 32.5 m/s (63 kt) | cyc | 41.624, -87.665 | 35 km | volume_mean |
| 21:53Z | 69 | 32.5 m/s (63 kt) | cyc | 41.342, -87.645 | 47 km | volume_mean |
| 21:59Z | 73 | 32.6 m/s (63 kt) | cyc | 41.343, -87.639 | 47 km | volume_mean |
| 22:04Z | 80 | 32.6 m/s (63 kt) | cyc | 41.367, -87.640 | 46 km | volume_mean |
| 22:09Z | 65 | 31.1 m/s (60 kt) | cyc | 41.724, -87.559 | 46 km | volume_mean |
| 22:15Z | 92 | 32.7 m/s (63 kt) | cyc | 41.814, -87.808 | 33 km | volume_mean |
| 22:20Z | 84 | 33.0 m/s (64 kt) | cyc | 41.322, -87.544 | 55 km | volume_mean |
| 22:25Z | 95 | 32.8 m/s (64 kt) | anti | 41.391, -87.947 | 26 km | volume_mean |
| 22:31Z | 98 | 32.9 m/s (64 kt) | anti | 41.502, -87.714 | 33 km | volume_mean |
| 22:36Z | 97 | 32.2 m/s (63 kt) | cyc | 41.553, -87.676 | 35 km | volume_mean |
| 22:42Z | 95 | 32.8 m/s (64 kt) | anti | 41.326, -87.649 | 48 km | volume_mean |
| 22:47Z | 106 | 29.8 m/s (58 kt) | anti | 41.531, -88.506 | 36 km | volume_mean |
| 22:52Z | 112 | 29.5 m/s (57 kt) | cyc | 41.552, -88.417 | 28 km | volume_mean |
| 22:58Z | 101 | 32.4 m/s (63 kt) | anti | 41.585, -87.460 | 52 km | volume_mean |

### Tightened in-app-style tuning (min_shear 0.012 s⁻¹, min_area 10 km²)

Same run through the exposed knobs only — 0–29 per volume, discrete features
tracking the line's approach; a persistent strong cyclonic couplet family
sits south/southeast of KLOT (range 30–50 km) through 21:42–22:58 Z:

| time | n | strongest vr_sr | sign | centroid (lat, lon) | range | motion |
|---|---|---|---|---|---|---|
| 20:48Z | 0 | — | — | — | — | — |
| 20:53Z | 3 | 26.0 m/s (51 kt) | cyc | 41.152, -88.805 | 78 km | volume_mean |
| 20:58Z | 1 | 25.2 m/s (49 kt) | cyc | 41.195, -88.908 | 82 km | volume_mean |
| 21:03Z | 2 | 29.4 m/s (57 kt) | cyc | 40.888, -88.894 | 105 km | track:1 |
| 21:07Z | 2 | 30.1 m/s (58 kt) | cyc | 40.937, -88.588 | 85 km | volume_mean |
| 21:12Z | 3 | 31.1 m/s (60 kt) | cyc | 40.969, -88.686 | 87 km | volume_mean |
| 21:16Z | 3 | 28.5 m/s (55 kt) | cyc | 41.006, -88.667 | 82 km | volume_mean |
| 21:21Z | 5 | 29.7 m/s (58 kt) | anti | 41.502, -88.511 | 37 km | volume_mean |
| 21:27Z | 2 | 28.9 m/s (56 kt) | cyc | 41.128, -88.352 | 57 km | volume_mean |
| 21:32Z | 3 | 33.0 m/s (64 kt) | cyc | 40.838, -88.699 | 99 km | volume_mean |
| 21:37Z | 2 | 30.5 m/s (59 kt) | cyc | 40.851, -88.570 | 93 km | volume_mean |
| 21:42Z | 6 | 32.8 m/s (64 kt) | cyc | 41.589, -87.714 | 31 km | volume_mean |
| 21:48Z | 10 | 32.5 m/s (63 kt) | cyc | 41.548, -87.605 | 40 km | volume_mean |
| 21:53Z | 9 | 31.7 m/s (62 kt) | cyc | 41.711, -87.650 | 38 km | volume_mean |
| 21:59Z | 11 | 32.6 m/s (63 kt) | cyc | 41.345, -87.638 | 47 km | volume_mean |
| 22:04Z | 11 | 31.8 m/s (62 kt) | cyc | 41.799, -87.657 | 42 km | volume_mean |
| 22:09Z | 14 | 31.1 m/s (60 kt) | cyc | 41.779, -87.674 | 39 km | volume_mean |
| 22:15Z | 11 | 32.6 m/s (63 kt) | anti | 41.844, -87.888 | 31 km | volume_mean |
| 22:20Z | 17 | 33.0 m/s (64 kt) | cyc | 41.325, -87.569 | 53 km | volume_mean |
| 22:25Z | 25 | 32.3 m/s (63 kt) | cyc | 41.864, -87.926 | 32 km | volume_mean |
| 22:31Z | 23 | 32.9 m/s (64 kt) | anti | 41.492, -87.639 | 39 km | volume_mean |
| 22:36Z | 25 | 32.2 m/s (63 kt) | anti | 41.566, -87.585 | 42 km | volume_mean |
| 22:42Z | 21 | 29.7 m/s (58 kt) | anti | 41.567, -87.539 | 46 km | volume_mean |
| 22:47Z | 29 | 29.7 m/s (58 kt) | cyc | 41.344, -87.591 | 50 km | volume_mean |
| 22:52Z | 24 | 29.1 m/s (57 kt) | anti | 41.604, -87.626 | 38 km | volume_mean |
| 22:58Z | 29 | 32.4 m/s (63 kt) | cyc | 41.412, -87.654 | 42 km | volume_mean |

Observations for the tuning pass (informational only):

- `vr_sr_ms` tops out ~32–33 m/s throughout — consistent with the ±32 m/s-ish
  velocity extremes the gridded layer carries (no dealiasing in this
  pipeline), so strong markers are effectively "extrema at the data's edge".
- `motion_source` is `volume_mean` for most leaders: the strongest shear
  rides the line segments away from the 1–6 SCIT cell seeds, beyond the
  10 km association radius. Raising `couplet_assoc_max_km` (or the future
  envelope-distance upgrade noted as a TODO in `_motion_for`) would attribute
  more couplets to specific tracks.
- This is a QLCS event; discrete-supercell cases should look much cleaner at
  the defaults.

## What changed, per file

- `detection/couplets.py` (new): NaN-aware azimuthal shear, component
  extraction, motion association, `vr_ms`/`vr_sr_ms` via `correct_vrot` (its
  first caller), deterministic ordering, sign-convention derivation.
- `settings/resolver.py`: `CoupletParams` (+`from_dict`), `couplets` /
  `couplet_marker_min_vrot_kt` projections.
- `settings/registry.py`: `GROUP_ROTATION`, five `couplet_*` keys,
  `COUPLET_KEYS` with the future-provenance comment; not in `DETECTION_KEYS`.
- `pipeline.py`: `VolumeResult.couplets` container field (default empty; no
  cache/format impact — `save_npz` never serialises results).
- `panes/map_client.py`: `set_couplets`/`clear_couplets` (GeoJSON, band
  thresholds 26/40 kt, sub-`min_vrot_kt` not sent), `pan_to`.
- `map_window_gtk.py`: `setCouplets`/`clearCouplets`, inverted-triangle SVG
  divIcons, CYCLONIC/ANTICYCLONIC ROTATION popups, `clearAll` includes the
  layer. No "tornado" text anywhere.
- `panes/left_volumes.py`: couplet child rows after Storm rows,
  `couplet_selected` signal.
- `app.py`: `_annotate_couplets` on the cells' lifecycle hooks (`_on_gridded`,
  `_reprocess_current`), `set_couplets` on every volume (re)render including
  scrub frames, layer cleared on warning switch, couplet row click → map pan.

## Constraint compliance

No SCIT/tracking/cache/SRM-module changes (SRM only imported); no meso
tracking, no PostGIS, association limited to the motion-vector lookup;
numpy/scipy/stdlib only; structlog events (`couplets.detected`,
`gui.couplet_error`); deterministic ordering asserted by test.
