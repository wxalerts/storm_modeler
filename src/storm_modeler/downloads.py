"""On-disk cache of downloaded warnings, so they persist across app launches.

Each downloaded warning gets a directory ``CACHE_DIR/downloads/<id>/`` holding:

* ``warning.json`` — the :class:`~storm_modeler.models.Warning` metadata.
* ``volumes/*.npz`` — the gridded radar volumes.

That is exactly the layout :class:`~storm_modeler.data.volumes.FixtureVolumeSource`
reads, so a persisted download replays through the identical pipeline (SCIT +
tracking) with no network and no re-gridding. On the next launch the app scans
this store to repopulate the sidebar's "Downloaded" list.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog

from .config import CACHE_DIR
from .data.volumes import FixtureVolumeSource
from .models import GriddedVolume, Warning

log = structlog.get_logger(__name__)

# Warning ids are filename-safe already (e.g. ``2024-05-25-KFWS-TOW-1234``);
# sanitise defensively so an unexpected id can never escape the cache root.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class DownloadStore:
    """Filesystem-backed registry of downloaded warnings + their volumes."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else (CACHE_DIR / "downloads")

    def _safe(self, warning_id: str) -> str:
        return _UNSAFE.sub("_", warning_id)

    def dir(self, warning_id: str) -> Path:
        return self.root / self._safe(warning_id)

    def has(self, warning_id: str) -> bool:
        return (self.dir(warning_id) / "warning.json").exists()

    # --- writes -----------------------------------------------------------

    def save_warning(self, warning: Warning) -> Path:
        """Create the warning's cache dir and write ``warning.json`` (idempotent)."""
        d = self.dir(warning.id)
        (d / "volumes").mkdir(parents=True, exist_ok=True)
        wfile = d / "warning.json"
        if not wfile.exists():
            wfile.write_text(json.dumps(warning.to_dict()))
        return d

    def save_volume(self, warning_id: str, volume: GriddedVolume) -> None:
        """Persist one gridded volume as ``volumes/vol_<valid_time>.npz``."""
        vdir = self.dir(warning_id) / "volumes"
        vdir.mkdir(parents=True, exist_ok=True)
        name = f"vol_{volume.valid_time:%Y%m%dT%H%M%SZ}.npz"
        volume.save_npz(vdir / name)

    # --- reads ------------------------------------------------------------

    def volume_source(self, warning_id: str) -> FixtureVolumeSource:
        """A :class:`FixtureVolumeSource` replaying this warning's cached volumes."""
        return FixtureVolumeSource(self.dir(warning_id))

    def volume_count(self, warning_id: str) -> int:
        return len(list((self.dir(warning_id) / "volumes").glob("*.npz")))

    def warnings(self) -> list[Warning]:
        """Every persisted warning, oldest→newest (skips unreadable dirs)."""
        out: list[Warning] = []
        if not self.root.exists():
            return out
        for d in sorted(self.root.iterdir()):
            wfile = d / "warning.json"
            if not wfile.exists():
                continue
            try:
                out.append(Warning.from_json_file(wfile))
            except Exception as e:  # noqa: BLE001 - a corrupt dir shouldn't break launch
                log.warning("downloads.load_failed", dir=str(d),
                            error=str(e).splitlines()[0])
        out.sort(key=lambda w: w.issued)
        return out
