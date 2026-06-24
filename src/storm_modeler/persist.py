"""PostGIS / TimescaleDB persistence.

Two tables, both with a production-matching schema:

* ``warnings_v2`` — one row per historical warning (geometry, VTEC fields,
  issued/expires).
* ``cells_v2`` — one row per SCIT cell per volume, tagged with ``warning_id``
  and ``event_type`` so cells join straight back to the warning that drove the
  pull.

Time columns are managed by TimescaleDB hypertables when the extension is
available; otherwise they fall back to plain b-tree indexes (the schema is
identical either way). All writes are idempotent upserts keyed on the natural
identity of the row, so reruns never duplicate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

import structlog

from .config import pg_dsn
from .detection.detection_v2 import StormCell
from .models import Warning

log = structlog.get_logger(__name__)


SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS warnings_v2 (
    id            text PRIMARY KEY,
    event         text NOT NULL,
    phenomena     text NOT NULL,
    significance  text NOT NULL,
    wfo           text,
    etn           integer,
    ugc           text[],
    states        text[],
    polygon       geometry(Polygon, 4326),
    issued        timestamptz NOT NULL,
    expires       timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS warnings_v2_issued_idx ON warnings_v2 (issued);
CREATE INDEX IF NOT EXISTS warnings_v2_polygon_gix ON warnings_v2 USING gist (polygon);

CREATE TABLE IF NOT EXISTS cells_v2 (
    id           bigserial,
    warning_id   text REFERENCES warnings_v2 (id) ON DELETE CASCADE,
    event_type   text,
    site         text NOT NULL,
    valid_time   timestamptz NOT NULL,
    track_id     integer NOT NULL,
    cell_id      integer NOT NULL,
    seed_lon     double precision,
    seed_lat     double precision,
    max_dbz      real,
    area_km2     real,
    echo_top_km  real,
    base_km      real,
    depth_km     real,
    n_levels     integer,
    centroid     geometry(Point, 4326),
    envelope     geometry(Polygon, 4326),
    PRIMARY KEY (id, valid_time),
    UNIQUE (warning_id, site, valid_time, track_id)
);
CREATE INDEX IF NOT EXISTS cells_v2_valid_time_idx ON cells_v2 (valid_time);
CREATE INDEX IF NOT EXISTS cells_v2_warning_idx ON cells_v2 (warning_id);
CREATE INDEX IF NOT EXISTS cells_v2_envelope_gix ON cells_v2 USING gist (envelope);
"""

# Attempt to make cells_v2 a Timescale hypertable on valid_time. No-op (and
# harmless) when the extension is unavailable — the table works regardless.
TIMESCALE_SQL = """
CREATE EXTENSION IF NOT EXISTS timescaledb;
SELECT create_hypertable('cells_v2', 'valid_time',
                         if_not_exists => TRUE, migrate_data => TRUE);
"""


class Persistence:
    """Thin idempotent writer over PostGIS. Use as a context manager."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or pg_dsn()
        if not self.dsn:
            raise RuntimeError("No PG_DSN configured; cannot persist.")
        self._conn = None
        self.timescale = False

    def __enter__(self) -> "Persistence":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        import psycopg

        self._conn = psycopg.connect(self.dsn, autocommit=False)
        self.ensure_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def ensure_schema(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        self._conn.commit()
        # Best-effort Timescale hypertable; degrade gracefully if absent.
        try:
            with self._conn.cursor() as cur:
                cur.execute(TIMESCALE_SQL)
            self._conn.commit()
            self.timescale = True
            log.info("persist.timescale_enabled")
        except Exception as e:  # noqa: BLE001
            self._conn.rollback()
            self.timescale = False
            log.info("persist.timescale_unavailable", reason=str(e).splitlines()[0])

    # --- writes -----------------------------------------------------------

    def upsert_warning(self, w: Warning) -> None:
        from shapely import wkb

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO warnings_v2
                    (id, event, phenomena, significance, wfo, etn, ugc, states,
                     polygon, issued, expires)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                        ST_GeomFromWKB(%s, 4326), %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    event=EXCLUDED.event,
                    phenomena=EXCLUDED.phenomena,
                    significance=EXCLUDED.significance,
                    wfo=EXCLUDED.wfo,
                    etn=EXCLUDED.etn,
                    ugc=EXCLUDED.ugc,
                    states=EXCLUDED.states,
                    polygon=EXCLUDED.polygon,
                    issued=EXCLUDED.issued,
                    expires=EXCLUDED.expires
                """,
                (
                    w.id, w.event, w.phenomena, w.significance, w.wfo, w.etn,
                    w.ugc, w.states, wkb.dumps(w.polygon), w.issued, w.expires,
                ),
            )
        self._conn.commit()

    def upsert_cells(
        self, warning_id: str, event_type: str, cells: Iterable[StormCell]
    ) -> int:
        from shapely import wkb
        from shapely.geometry import Point

        n = 0
        with self._conn.cursor() as cur:
            for c in cells:
                pt = Point(c.seed_lon, c.seed_lat)
                cur.execute(
                    """
                    INSERT INTO cells_v2
                        (warning_id, event_type, site, valid_time, track_id,
                         cell_id, seed_lon, seed_lat, max_dbz, area_km2,
                         echo_top_km, base_km, depth_km, n_levels,
                         centroid, envelope)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            ST_GeomFromWKB(%s,4326), ST_GeomFromWKB(%s,4326))
                    ON CONFLICT (warning_id, site, valid_time, track_id)
                    DO UPDATE SET
                        event_type=EXCLUDED.event_type,
                        cell_id=EXCLUDED.cell_id,
                        seed_lon=EXCLUDED.seed_lon,
                        seed_lat=EXCLUDED.seed_lat,
                        max_dbz=EXCLUDED.max_dbz,
                        area_km2=EXCLUDED.area_km2,
                        echo_top_km=EXCLUDED.echo_top_km,
                        base_km=EXCLUDED.base_km,
                        depth_km=EXCLUDED.depth_km,
                        n_levels=EXCLUDED.n_levels,
                        centroid=EXCLUDED.centroid,
                        envelope=EXCLUDED.envelope
                    """,
                    (
                        warning_id, event_type, c.site, c.valid_time, c.track_id,
                        c.cell_id, c.seed_lon, c.seed_lat, c.max_dbz, c.area_km2,
                        c.echo_top_km, c.base_km, c.depth_km, c.n_levels,
                        wkb.dumps(pt), wkb.dumps(c.envelope),
                    ),
                )
                n += 1
        self._conn.commit()
        return n
