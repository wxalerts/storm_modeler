# WxAlerts Storm Modeler

A desktop validation harness around the **SCIT** storm-cell identification &
tracking package (`detection_v2`, the code under test). IEM historical warnings
drive archived NEXRAD volume downloads; each volume is gridded with Py-ART, run
through SCIT, and the results render across a multi-window GUI — a Leaflet radar
map, an interactive 3D storm view, and a live log — and optionally land in
PostGIS. **Every SCIT tunable is editable at runtime** from a settings dialog;
no code changes to tune. No alert dispatch — this is an analysis/validation tool.

Historical + deterministic: each warning is a fixed-window event with fully
archived radar behind it, so a re-run yields identical cells. Every detection
row carries a `settings_hash` tracing it to the exact knob set that produced it.

## Features

- **5-window, GIMP-style layout** (each a separate top-level window):
  - **Data** — IEM search, the Downloaded list, and the per-volume storm list (the hub; closing it quits).
  - **Map** — a real **Leaflet** map with **ESRI** imagery tiles. It runs as its own **WebKitGTK** process (Qt WebEngine renders black on some Linux GL stacks), driven over a pipe. Radar overlay + warning polygon + SCIT cell envelopes + GLM lightning markers are all placed by lat/lon, so they line up exactly. Pan/drag + scroll-zoom, no 3D rotation.
  - **3D View** — VTK perspective volume render, isosurfaces, envelope prism, echo-top marker, and a vertical cross-section, framed on the selected cell.
  - **Lightning** — fetches **GOES GLM** flashes for the selected warning's window and layers them on the Map as markers (see below).
  - **Logs** — a live tail of the run's log.
- **Full dual-pol product cycling** — Reflectivity, Velocity, Spectrum Width, ZDR, Correlation Coefficient, Differential Phase, each with its own color scale. Cycle with the **Product** menu or the **P** key; the moment is labeled on the map. (Reflectivity is the column-max composite; the other moments are the lowest-tilt layer.)
- **Synced Map + 3D playback loop** — the scrubber steps through a warning's volumes and loops; the map overlay and the 3D view advance in lock-step.
- **GOES GLM lightning overlay** — the Lightning window pulls L2 `LCFA` flashes for the selected warning's data window from the `noaa-goes16`/`noaa-goes19` (GOES-East) S3 buckets (anonymous boto3, threaded downloads — the same fast path as the radar pull), keeps the flashes inside the warning's padded bounding box, and layers them on the Leaflet map as canvas markers ramped oldest→newest. Bounding-box padding, the marker cap, and the quality filter are runtime-tunable.
- **Fast, multi-threaded downloads** — anonymous reads from the Unidata `unidata-nexrad-level2` S3 mirror (boto3, unsigned), fetched concurrently with a bounded look-ahead while gridding/yielding stays in order (SCIT tracking is order-sensitive). ~30× faster than the THREDDS HTTP path on a typical link.
- **Runtime-tunable detection** — every threshold/window/display pref is a `SettingSpec` resolved from registry-defaults ⊕ PostGIS overrides into a typed `DetectionParams`; no tunable is a module constant.
- **Diagnostics to stdout** — structlog tees to stdout + a per-run log file, with `faulthandler` and excepthooks installed so a native crash (VTK/PROJ) or an escaped exception is captured rather than vanishing.

## Architecture

```
settings/registry.py   SettingSpec list — the single source of every tunable
settings/resolver.py   defaults ⊕ PostGIS overrides -> DetectionParams (+ hash)
        │
WarningSource ─ IEMHistoricalSource (IEM SBW archive, TO/SV·W; pyogrio raw read)  data/warnings.py
        │  SiteResolver (full CONUS WSR-88D table)                                data/sites.py
VolumeSource ─ S3Level2Source (unidata-nexrad-level2, boto3 unsigned, threaded)   data/volumes.py
             ├ ThreddsLevel2Source (anonymous HTTP fallback)
             └ FixtureVolumeSource (replay pre-gridded .npz — also the download cache)
        │  grid_level2 → all dual-pol moments → detection_v2.run(volume, params)
detection/detection_v2/  identify.py · track.py   ← SCIT, under test, no globals
        │
        ├─▶ persist: warnings_v2 + cells_v2 (+ settings_hash), per-volume commit
        └─▶ Qt signals → Data / 3D / Logs windows  +  pipe → GTK Leaflet map
```

Gridding/SCIT run on a `QThreadPool`; SCIT detection itself runs on the GUI
thread (PROJ is not safe to first-init on a worker). Downloads commit **per
volume**; a checked cancel `Event` stops further fetching while keeping
everything already committed. Downloaded warnings persist to a local cache and
replay offline through the identical pipeline.

## Setup

```bash
uv sync                 # core + GUI deps
uv sync --extra live    # + arm-pyart / boto3 / s3fs / httpx / geopandas (live pulls)

# Qt/VTK system libs (Debian/Ubuntu):
sudo apt-get install -y libegl1 libgl1 libglx-mesa0 libgl1-mesa-dri libosmesa6 \
  libxkbcommon0 libxcb-cursor0 libxrender1 libfontconfig1 xvfb

# The Leaflet map window uses WebKitGTK (runs under the system python3):
sudo apt-get install -y python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 libwebkit2gtk-4.1-0

# Optional PostGIS persistence:
createdb storm_modeler && psql storm_modeler -c "CREATE EXTENSION postgis;"
export PG_DSN="postgresql:///storm_modeler?host=/var/run/postgresql"
```

Notable env vars: `PG_DSN` (persistence; omit to run without a DB),
`STORM_MODELER_DL_WORKERS` (concurrent downloads, default 4),
`STORM_MODELER_GTK_PYTHON` (interpreter for the map process, default
`/usr/bin/python3`), `STORM_MODELER_LOG_DIR`, `STORM_MODELER_CACHE`.

## Run

```bash
uv run python -m storm_modeler.app                        # interactive GUI
uv run python -m storm_modeler.app --from 2024-05-25T17:00Z --to 2024-05-25T19:00Z
uv run python -m storm_modeler.app --headless --replay <dir> [--persist]   # no-GUI replay
```

Search a warning, hit **Download**, then click a storm to drive the 3D view +
map. Press **P** to cycle products; use the scrubber to loop the volumes.

## Tests

```bash
uv run pytest                                              # unit + integration
QT_QPA_PLATFORM=offscreen uv run python -m storm_modeler.app --smoke   # builds every pane, exit 0
```

Tests are hermetic and need no network: the deterministic replay cases are
generated on the fly into a temp dir by `data/synthetic.py` (no synthetic data
ships in the repo, so it is never mistaken for a real download).
