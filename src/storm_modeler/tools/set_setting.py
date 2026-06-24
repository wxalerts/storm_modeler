"""Set a runtime setting override in PostGIS.

    uv run python -m storm_modeler.tools.set_setting <key> <value>

Validates the value against the registry spec (type, min/max, choices) and
upserts it into ``app_settings``. The next resolve/run picks it up — no code
change. This is the round-trip that Section 8A drives detection with.
"""

from __future__ import annotations

import sys

from ..settings.registry import get_spec
from ..settings.store import SettingsStore


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: set_setting <key> <value>", file=sys.stderr)
        print("keys:", ", ".join(s.key for s in __import__(
            "storm_modeler.settings.registry", fromlist=["REGISTRY"]).REGISTRY),
            file=sys.stderr)
        return 2

    key, raw = argv
    try:
        get_spec(key)  # fail fast on unknown key
    except KeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        with SettingsStore() as store:
            clean = store.set(key, raw)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"set {key} = {clean!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
