"""Settings store — runtime overrides of registry defaults in PostGIS.

Backed by ``app_settings(key text primary key, value jsonb, updated_at
timestamptz)``. Only overridden keys live here; everything else falls back to
the registry default at resolve time.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from ..config import local_settings_path, pg_dsn
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


class LocalSettingsStore:
    """File-backed overrides — the no-database fallback for ``SettingsStore``.

    Persists overrides to a JSON file so the desktop app keeps its tuning across
    restarts without PostGIS. Mirrors the ``SettingsStore`` interface
    (``overrides`` / ``set`` / ``set_many`` / ``unset``, context-manager friendly)
    so callers are storage-agnostic.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else local_settings_path()
        self._data: dict[str, Any] | None = None

    def __enter__(self) -> "LocalSettingsStore":
        self._data = self._read()
        return self

    def __exit__(self, *exc) -> None:
        self._data = None

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2, sort_keys=True))

    def overrides(self) -> dict[str, Any]:
        data = self._data if self._data is not None else self._read()
        out: dict[str, Any] = {}
        for key, value in data.items():
            try:
                out[key] = get_spec(key).validate(value)
            except (KeyError, ValueError) as e:  # noqa: PERF203
                log.warning("settings.skip_override", key=key, reason=str(e))
        return out

    def set(self, key: str, value: Any) -> Any:
        return self.set_many({key: value})[key]

    def set_many(self, items: dict[str, Any]) -> dict[str, Any]:
        if self._data is None:
            self._data = self._read()
        cleaned: dict[str, Any] = {}
        for key, value in items.items():
            cleaned[key] = get_spec(key).validate(value)
            self._data[key] = cleaned[key]
        self._write()
        log.info("settings.set_local", keys=list(cleaned), path=str(self.path))
        return cleaned

    def unset(self, key: str) -> None:
        if self._data is None:
            self._data = self._read()
        self._data.pop(key, None)
        self._write()


def open_store(
    dsn: str | None = None, local_path: str | Path | None = None
) -> "SettingsStore | LocalSettingsStore | None":
    """Pick a settings store: PostGIS if a DSN is given, else a local JSON file.

    Returns ``None`` only when neither is requested (pure-defaults callers).
    """
    if dsn:
        return SettingsStore(dsn)
    if local_path:
        return LocalSettingsStore(local_path)
    return None
