# WxAlerts Storm Modeler — Phase A

A 3-pane desktop validation harness around the **SCIT** storm-cell
identification & tracking package (`detection_v2`, the code under test).
Historical NWS warnings drive archived radar-volume pulls, volumes run through
SCIT, and results render on a self-rendered top-down map and a clickable nav
tree, landing in PostGIS.

Phase A is **historical and deterministic**: every warning is a fixed-window
event with fully archived radar behind it, so the entire pipeline is
reproducible — no live clocks, no tailing, no network races. There is no 3D
model view yet (Phase B) and **no alert dispatch ever**.

## Architecture

```
WarningSource ─ IEMHistoricalSource (IEM SBW archive, TO/SV · W)   data/warnings.py
              └ FixtureWarningSource (offline replay)
     │
     ▼  SiteResolver (full CONUS WSR-88D table)                    data/sites.py
VolumeSource ─ NexradArchiveSource (noaa-nexrad-level2 + Py-ART)   data/volumes.py
             └ FixtureVolumeSource (pre-gridded .npz)
     │
     ▼  grid → detection_v2.run(volume) → cells   (off the GUI thread)
detection/detection_v2/  identify.py · track.py   ← SCIT, under test
     │
     ├─▶ persist (cells_v2 + warnings_v2, idempotent)              persist.py
     └─▶ Qt signals → nav tree + self-rendered map                 panes/
```

* **Map** is rendered in planar lon/lat: a vector basemap (counties + states +
  highways) from shipped vector files, the **self-rendered** composite
  reflectivity from the gridded volume (NWS dBZ colours — never a tile server),
  cell envelopes, and the warning polygon.
* **Workers** run all I/O + gridding + SCIT + persistence on a `QThreadPool`; the
  GUI thread never blocks.

## Setup

```bash
uv sync                 # core + GUI deps (offline pipeline + offscreen GUI)
uv sync --extra live    # + arm-pyart / boto3 / s3fs / httpx / geopandas (live pulls)
```

System libraries for the Qt/VTK GUI (Debian/Ubuntu):

```bash
apt-get install -y libegl1 libgl1 libglx-mesa0 libgl1-mesa-dri libosmesa6 \
  libxkbcommon0 libxcb-cursor0 libxrender1 libfontconfig1
```

PostGIS (local), then point `PG_DSN` at it:

```bash
createdb storm_modeler && psql storm_modeler -c "CREATE EXTENSION postgis;"
export PG_DSN="postgresql:///storm_modeler?host=/var/run/postgresql"
```

TimescaleDB is used for the time columns when the extension is available; the
schema degrades gracefully to plain b-tree indexes otherwise.

## Regenerate shipped data / fixtures

```bash
uv run python scripts/make_basemap.py    # data/shapefiles/*.geojson
uv run python scripts/make_fixtures.py   # tests/fixtures/{tornado_warning_case,ap_case}
```

## Run

```bash
# Interactive GUI (use Xvfb on a headless host)
uv run python -m storm_modeler.app
uv run python -m storm_modeler.app --replay src/storm_modeler/tests/fixtures/tornado_warning_case

# Live IEM historical pull (needs `--extra live` + archive network access)
uv run python -m storm_modeler.app --from 2024-05-25T17:00Z --to 2024-05-25T19:00Z
```

## Validation (Section 7)

```bash
# A. historical replay of one tornado warning → cells joined to the warning
uv run python -m storm_modeler.app \
  --headless --replay src/storm_modeler/tests/fixtures/tornado_warning_case/ --persist
psql "$PG_DSN" -c \
  "select w.event, count(distinct c.track_id) storms, max(c.depth_km) d
   from warnings_v2 w join cells_v2 c on c.warning_id = w.id group by w.event;"

# B. AP false-seed audit — must yield ZERO admitted cells
uv run python -m storm_modeler.app \
  --headless --replay src/storm_modeler/tests/fixtures/ap_case/ --persist
psql "$PG_DSN" -c \
  "select count(*) from cells_v2
   where site='KHGX' and valid_time='2026-06-24 11:41:00Z';"   # -> 0

# C. GUI offscreen smoke — builds all panes without a display
QT_QPA_PLATFORM=offscreen uv run python -m storm_modeler.app --smoke   # -> exit 0

# Unit + integration tests
QT_QPA_PLATFORM=offscreen uv run pytest
```

## Notes on this environment

The Phase-A pipeline is exercised entirely offline through the deterministic
fixtures (the IEM and S3 archives are not reachable from the build container,
and real Level II volumes are neither deterministic nor small). The live
`IEMHistoricalSource` / `NexradArchiveSource` are implemented to spec and
lazily import the `live` extra so they cost nothing on the offline path.
