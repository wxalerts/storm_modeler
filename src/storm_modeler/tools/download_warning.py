"""Download + process a warning's volumes, with optional mid-run cancel.

    uv run python -m storm_modeler.tools.download_warning --fixture <name> [--cancel-after N]

Drives the per-volume commit path against a replay fixture (offline). With
``--cancel-after N``, the cancel event is tripped once N volumes are committed:
processing stops before the next volume, and every volume already committed
(persisted to ``cells_v2``) remains — no rollback. This is the headless
equivalent of the GUI download dialog's Cancel button.
"""

from __future__ import annotations

import argparse
import sys
import threading
from pathlib import Path

from ..config import FIXTURE_DIR, pg_dsn
from ..pipeline import VolumeResult, replay_fixture


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="download_warning")
    p.add_argument("--fixture", required=True, help="fixture name under tests/fixtures")
    p.add_argument("--cancel-after", type=int, default=0,
                   help="cancel after N committed volumes (0 = no cancel)")
    p.add_argument("--no-persist", action="store_true", help="skip PostGIS writes")
    args = p.parse_args(argv)

    fixture_dir = Path(args.fixture)
    if not fixture_dir.exists():
        fixture_dir = FIXTURE_DIR / args.fixture
    if not fixture_dir.exists():
        print(f"error: fixture not found: {args.fixture}", file=sys.stderr)
        return 2

    persist = not args.no_persist
    dsn = pg_dsn() if persist else None
    if persist and not dsn:
        print("error: --persist needs PG_DSN", file=sys.stderr)
        return 2

    cancel = threading.Event()
    committed = {"n": 0}

    def on_result(res: VolumeResult) -> None:
        committed["n"] += 1
        if args.cancel_after and committed["n"] >= args.cancel_after:
            cancel.set()  # tripped after this volume is committed

    summary = replay_fixture(
        fixture_dir, persist=persist, dsn=dsn, on_result=on_result, cancel=cancel
    )
    print(
        f"committed volumes={summary.volumes} cells={summary.cells} "
        f"persisted={summary.persisted_cells} "
        f"cancelled_after={args.cancel_after or 'n/a'} "
        f"settings_hash={summary.settings_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
