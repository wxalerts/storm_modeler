# WxAlerts Storm Modeler — Phase A

A 3-pane desktop validation harness around the **SCIT** storm-cell
identification & tracking package (`detection_v2`, the code under test). IEM
**historical** warnings drive archived radar-volume downloads, volumes run
through SCIT, results render on a self-rendered top-down map and a clickable
nav, and land in PostGIS. **Every SCIT tunable is editable at runtime** from a
settings dialog backed by PostGIS — no code changes to tune. No 3D yet (Phase
B); no alert dispatch ever.

Historical + deterministic: each warning is a fixed-window event with fully
archived radar behind it, so the pipeline is reproducible — re-running a warning
yields identical cells. Every detection row carries a `settings_hash` so it is
traceable to the exact knob set that produced it.

## Architecture

```
settings/registry.py   SettingSpec list — the single source of every tunable
settings/store.py      app_settings (PostGIS) — runtime overrides
settings/resolver.py   defaults ⊕ overrides -> DetectionParams (+ settings_hash)
        │
WarningSource ─ IEMHistoricalSource (IEM SBW archive, TO/SV·W)   data/warnings.py
              └ FixtureWarningSource (offline replay)
        │  SiteResolver (full CONUS WSR-88D table)                data/sites.py
VolumeSource ─ NexradArchiveSource (noaa-nexrad-level2 + Py-ART)  data/volumes.py
             └ FixtureVolumeSource (pre-gridded .npz)
        │  grid → detection_v2.run(volume, params) → cells    (off the GUI thread)
detection/detection_v2/  identify.py · track.py   ← SCIT, under test, no globals
        │
        ├─▶ persist: warnings_v2 + cells_v2 (+ settings_hash), per-volume commit
        └─▶ Qt signals → search/volumes panes + self-rendered map
```

* **No tunable is a module constant** — detection thresholds, data-window
  minutes, IEM defaults, and display prefs are all `SettingSpec`s in the
  registry. The resolver merges DB overrides over registry defaults into a typed
  `DetectionParams`, which is passed explicitly to `detection_v2.run`.
* **Workers** run all I/O + gridding + SCIT + persistence on a `QThreadPool`;
  downloads commit **per volume** and a checked cancel `Event` stops further
  fetching while keeping everything already committed.

## GUI (A2)

`QMainWindow` → horizontal splitter:

1. **Left panel** — a vertical splitter: IEM **search** form + results (each
   result has a `[Download]` button) on top; the selected warning's **volumes +
   detections** below (storm click → map recenter/highlight).
2. **Map** — VTK orthographic top-down: vector basemap, self-rendered composite
   reflectivity (NWS dBZ colours, never a tile server), cell envelopes, warning
   polygon, selected-cell highlight.
3. **Model** — Phase B placeholder.

**Settings dialog**: a top search box filters the list live; a scrolling table
with type-aware editors (spin/checkbox/combo/line edit, min/max/choice
validated); a Save button writes only changed keys to `app_settings`, reloads
the resolver, and re-applies on the next run.

## Setup

```bash
uv sync                 # core + GUI deps (offline pipeline + offscreen GUI)
uv sync --extra live    # + arm-pyart / boto3 / s3fs / httpx / geopandas (live pulls)

# Qt/VTK system libs (Debian/Ubuntu) + Xvfb for headless rendering:
apt-get install -y libegl1 libgl1 libglx-mesa0 libgl1-mesa-dri libosmesa6 \
  libxkbcommon0 libxcb-cursor0 libxrender1 libfontconfig1 xvfb

createdb storm_modeler && psql storm_modeler -c "CREATE EXTENSION postgis;"
export PG_DSN="postgresql:///storm_modeler?host=/var/run/postgresql"
```

TimescaleDB is used for the time columns when available; the schema degrades
gracefully to plain b-tree indexes otherwise.

## Run

```bash
uv run python -m storm_modeler.app                         # interactive GUI
uv run python -m storm_modeler.app --replay <fixture_dir>  # GUI preloaded w/ fixtures
uv run python -m storm_modeler.app --from 2024-05-25T17:00Z --to 2024-05-25T19:00Z
```

## Validation

### 8A — A1 headless spine

```bash
# settings round-trip drives detection (no code edit):
uv run python -m storm_modeler.tools.set_setting seed_dbz 70
uv run python -m storm_modeler.app --headless --replay src/storm_modeler/tests/fixtures/tornado_warning_case/ --persist
psql "$PG_DSN" -c "select count(*) from cells_v2;"           # -> 0 (peak < 70 dBZ)
uv run python -m storm_modeler.tools.set_setting seed_dbz 50
uv run python -m storm_modeler.app --headless --replay src/storm_modeler/tests/fixtures/tornado_warning_case/ --persist
psql "$PG_DSN" -c "select w.event, count(distinct c.track_id) storms, max(c.depth_km) d
  from warnings_v2 w join cells_v2 c on c.warning_id=w.id group by w.event;"   # realistic storms

# IEM filter admits only TO/SV·W (needs --extra live + network):
uv run python -m storm_modeler.tools.iem_query --sts 2024-05-06T18:00Z --ets 2024-05-07T06:00Z --dump

# AP false-seed audit:
uv run python -m storm_modeler.app --headless --replay src/storm_modeler/tests/fixtures/ap_case/ --persist
psql "$PG_DSN" -c "select count(*) from cells_v2 where site='KHGX' and valid_time='2026-06-24 11:41:00Z';"  # -> 0

# cancel keeps committed volumes:
uv run python -m storm_modeler.tools.download_warning --fixture tornado_warning_case --cancel-after 5
psql "$PG_DSN" -c "select count(distinct valid_time) from cells_v2 where warning_id=(select id from warnings_v2 limit 1);"  # -> 5
```

### 8B — A2 GUI

```bash
QT_QPA_PLATFORM=offscreen uv run python -m storm_modeler.app --smoke   # exit 0; all panes + dialogs instantiate

QT_QPA_PLATFORM=offscreen uv run pytest    # unit + integration suite
```

## Regenerate shipped data / fixtures

```bash
uv run python scripts/make_basemap.py    # data/shapefiles/*.geojson
uv run python scripts/make_fixtures.py   # tests/fixtures/{tornado_warning_case,ap_case}
```

## Notes on this environment

The pipeline is exercised entirely offline through the deterministic fixtures
(the IEM and S3 archives are unreachable from the build container, and real
Level II volumes are neither deterministic nor small). The live
`IEMHistoricalSource` / `NexradArchiveSource` are implemented to spec and lazily
import the `live` extra so they cost nothing on the offline path. The offscreen
`--smoke` starts a private Xvfb so VTK renders against a stable software-GL
context regardless of the host's offscreen-GL quality.
