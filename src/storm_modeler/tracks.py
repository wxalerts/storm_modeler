"""Reconstruct unique storm tracks from a warning's per-volume detections.

SCIT assigns each storm cell a ``track_id`` that follows the same storm across a
warning's volumes (``-1`` until/unless the tracker links it). This module folds a
list of :class:`~storm_modeler.pipeline.VolumeResult` into one
:class:`StormTrack` per storm — a time-ordered history of the cell's reflectivity,
echo-top height, cloud-top temperature and overshooting-top flag, plus its track
position — which the Storm Track window lists and charts and the map draws.

It is pure (no GUI, no I/O) so it is cheap to call on every new volume and easy
to test. Cells the tracker never linked (``track_id == -1``) each become their
own single-sample track so nothing is silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TrackSample:
    """One volume's view of a tracked storm (a point in its history)."""

    valid_time: datetime
    seed_lon: float
    seed_lat: float
    max_dbz: float
    echo_top_km: float
    depth_km: float
    cloud_top_c: float | None  # None until a GOES ABI scene is associated
    overshooting_top: bool


@dataclass
class StormTrack:
    """A unique storm followed across a warning's volumes."""

    key: str  # stable identity across refreshes (track id, or per-cell for -1)
    track_id: int  # SCIT track id; -1 for an unlinked single-volume cell
    site: str
    samples: list[TrackSample] = field(default_factory=list)

    @property
    def n_volumes(self) -> int:
        return len(self.samples)

    @property
    def start_time(self) -> datetime:
        return self.samples[0].valid_time

    @property
    def end_time(self) -> datetime:
        return self.samples[-1].valid_time

    @property
    def peak_dbz(self) -> float:
        return max(s.max_dbz for s in self.samples)

    @property
    def max_echo_top_km(self) -> float:
        return max(s.echo_top_km for s in self.samples)

    @property
    def ever_overshooting(self) -> bool:
        return any(s.overshooting_top for s in self.samples)

    @property
    def min_cloud_top_c(self) -> float | None:
        cts = [s.cloud_top_c for s in self.samples if s.cloud_top_c is not None]
        return min(cts) if cts else None

    def label(self) -> str:
        """One-line summary for the track list."""
        if self.track_id >= 0:
            head = f"Track {self.track_id}"
        else:
            head = f"Cell @ {self.start_time:%H%M}Z"
        parts = [
            head,
            f"{self.n_volumes} vol",
            f"peak {self.peak_dbz:.0f} dBZ",
            f"top {self.max_echo_top_km:.0f} km",
        ]
        ct = self.min_cloud_top_c
        if ct is not None:
            parts.append(f"{ct:.0f}°C CT")
        if self.ever_overshooting:
            parts.append("OT")
        return "  ".join(parts)


def _sample(cell) -> TrackSample:
    return TrackSample(
        valid_time=cell.valid_time,
        seed_lon=cell.seed_lon,
        seed_lat=cell.seed_lat,
        max_dbz=cell.max_dbz,
        echo_top_km=cell.echo_top_km,
        depth_km=cell.depth_km,
        cloud_top_c=cell.cloud_top_c,
        overshooting_top=cell.overshooting_top,
    )


def build_tracks(results) -> list[StormTrack]:
    """Group a warning's per-volume cells into unique, time-ordered tracks.

    ``results`` is the ordered list of :class:`VolumeResult` for one warning.
    Cells sharing a ``track_id >= 0`` collapse into one track; each unlinked
    cell (``track_id == -1``) becomes its own single-sample track. Tracks come
    back sorted strongest-first (peak reflectivity), so the most significant
    storms top the list.
    """
    tracked: dict[int, StormTrack] = {}
    singletons: list[StormTrack] = []
    for res in sorted(results, key=lambda r: r.volume.valid_time):
        for cell in res.cells:
            if cell.track_id >= 0:
                track = tracked.get(cell.track_id)
                if track is None:
                    track = StormTrack(
                        key=f"t{cell.track_id}", track_id=cell.track_id,
                        site=cell.site,
                    )
                    tracked[cell.track_id] = track
                track.samples.append(_sample(cell))
            else:
                singletons.append(StormTrack(
                    key=f"u{res.volume.valid_time.isoformat()}#{cell.cell_id}",
                    track_id=-1, site=cell.site, samples=[_sample(cell)],
                ))
    tracks = list(tracked.values()) + singletons
    for t in tracks:
        t.samples.sort(key=lambda s: s.valid_time)
    tracks.sort(key=lambda t: t.peak_dbz, reverse=True)
    return tracks
