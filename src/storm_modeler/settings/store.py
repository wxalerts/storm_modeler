"""Settings store — runtime overrides of registry defaults in PostGIS.

Backed by ``app_settings(key text primary key, value jsonb, updated_at
timestamptz)``. Only overridden keys live here; everything else falls back to
the registry default at resolve time.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from ..config import pg_dsn
from .registry import get_spec

log = structlog.get_logger(__name__)

APP_SETTINGS_SQL = """
CREATE TABLE IF NOT EXISTS app_settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
"""


class SettingsStore:
    """Read/write overrides in ``app_settings``. Context-manager friendly."""

    def __init__(self, dsn: str | None = None) -> None:
        self.dsn = dsn or pg_dsn()
        if not self.dsn:
            raise RuntimeError("No PG_DSN configured; cannot access settings store.")
        self._conn = None

    def __enter__(self) -> "SettingsStore":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        import psycopg

        self._conn = psycopg.connect(self.dsn, autocommit=False)
        with self._conn.cursor() as cur:
            cur.execute(APP_SETTINGS_SQL)
        self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- reads ------------------------------------------------------------

    def overrides(self) -> dict[str, Any]:
        """All stored overrides, validated against the registry (bad/unknown
        rows are skipped with a warning rather than poisoning a run)."""
        with self._conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_settings")
            rows = cur.fetchall()
        out: dict[str, Any] = {}
        for key, value in rows:
            try:
                out[key] = get_spec(key).validate(value)
            except (KeyError, ValueError) as e:  # noqa: PERF203
                log.warning("settings.skip_override", key=key, reason=str(e))
        return out

    # --- writes -----------------------------------------------------------

    def set(self, key: str, value: Any) -> Any:
        """Validate ``value`` against the registry spec and upsert it."""
        clean = get_spec(key).validate(value)
        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (%s, %s::jsonb, now())
                ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, updated_at = now()
                """,
                (key, json.dumps(clean)),
            )
        self._conn.commit()
        log.info("settings.set", key=key, value=clean)
        return clean

    def set_many(self, items: dict[str, Any]) -> dict[str, Any]:
        cleaned = {}
        for key, value in items.items():
            cleaned[key] = self.set(key, value)
        return cleaned

    def unset(self, key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
        self._conn.commit()
