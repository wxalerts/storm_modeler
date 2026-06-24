"""Query the IEM historical warning archive and dump the admitted rows.

    uv run python -m storm_modeler.tools.iem_query --sts <ISO8601Z> --ets <ISO8601Z> \
        [--states TX,OK] [--dump]

Prints one line per warning the source admits. Because the source filters in
code to VTEC phenomena ``TO``/``SV`` and significance ``W``, every row printed
is guaranteed to satisfy that filter — ``--dump`` shows the phenomena/significance
so it is checkable.

Requires the ``live`` extra and archive network access; pulled sets are cached
on disk so a second run of the same query is offline.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from ..config import ADMITTED_PHENOMENA, ADMITTED_SIGNIFICANCE
from ..data.warnings import IEMHistoricalSource


def _parse_z(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="iem_query")
    p.add_argument("--sts", required=True, help="start time ISO8601Z")
    p.add_argument("--ets", required=True, help="end time ISO8601Z")
    p.add_argument("--states", default="", help="comma-separated state codes")
    p.add_argument("--dump", action="store_true", help="print phenomena/significance")
    args = p.parse_args(argv)

    states = [s.strip().upper() for s in args.states.split(",") if s.strip()]
    source = IEMHistoricalSource(_parse_z(args.sts), _parse_z(args.ets), states or None)

    try:
        warnings = list(source.warnings())
    except ImportError as e:
        print(
            f"error: live extra not installed ({e}). "
            "Run `uv sync --extra live`.", file=sys.stderr,
        )
        return 3
    except Exception as e:  # noqa: BLE001 - network/parse failures on offline hosts
        print(f"error: IEM query failed ({type(e).__name__}: {e})", file=sys.stderr)
        return 1

    bad = 0
    for w in warnings:
        ok = w.phenomena in ADMITTED_PHENOMENA and w.significance == ADMITTED_SIGNIFICANCE
        bad += 0 if ok else 1
        if args.dump:
            print(
                f"{w.phenomena}.{w.significance}  {w.event}  {w.wfo}  ETN {w.etn:04d}  "
                f"{w.issued:%Y-%m-%dT%H:%MZ}–{w.expires:%H:%MZ}  {','.join(w.states)}"
            )
        else:
            print(f"{w.event}  {w.wfo}  ETN {w.etn:04d}  {','.join(w.states)}")

    print(f"# {len(warnings)} warnings; all TO/SV·W: {bad == 0}", file=sys.stderr)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
