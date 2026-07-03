"""Build the AWIPS velocity LUT asset from the baseline ``8-bit Vel.cmap``.

Regenerates ``src/storm_modeler/assets/awips_vel_lut.npz`` — the checked-in
color table the velocity/SRM map products render through — from the official
NWS/AWIPS baseline colormap in Unidata's awips2 repository (pinned tag). The
``.cmap`` is XML with 256 ``<color r g b a>`` entries in [0, 1]; the raw XML is
not vendored, only the parsed array. Index semantics:

* index 0 — no data (fully transparent),
* index 1 — range folded (purple #7F00CF),
* indices 2–255 — the linear data ramp (magenta → cyan → green inbound,
  desaturated gray at zero, red → yellow → white outbound).

Usage (network fetch, or point ``--source`` at a downloaded copy)::

    uv run python -m storm_modeler.tools.build_awips_vel_lut
    uv run python -m storm_modeler.tools.build_awips_vel_lut --source /tmp/Vel.cmap
"""

from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

import numpy as np

from ..config import PACKAGE_ROOT

#: AWIPS baseline colormap, pinned to the unidata_17.1.1 tag for reproducibility.
DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/Unidata/awips2/unidata_17.1.1/"
    "edexOsgi/com.raytheon.uf.common.dataplugin.radar/utility/common_static/"
    "base/colormaps/Radar/8-bit%20Vel.cmap"
)
DEFAULT_OUT = PACKAGE_ROOT / "assets" / "awips_vel_lut.npz"

_COLOR = re.compile(
    r'<color\s+r\s*=\s*"([\d.]+)"\s+g\s*=\s*"([\d.]+)"'
    r'\s+b\s*=\s*"([\d.]+)"\s+a\s*=\s*"([\d.]+)"'
)


def parse_cmap(xml_text: str) -> np.ndarray:
    """Parse an AWIPS ``.cmap`` into a (256, 4) float64 RGBA array in [0, 1]."""
    rows = _COLOR.findall(xml_text)
    if len(rows) != 256:
        raise ValueError(f"expected 256 <color> entries, found {len(rows)}")
    lut = np.array(rows, dtype=np.float64)
    if lut.min() < 0.0 or lut.max() > 1.0:
        raise ValueError("color components outside [0, 1]")
    # Sanity-lock the index semantics this asset promises.
    if lut[0, 3] != 0.0:
        raise ValueError("index 0 must be transparent (no data)")
    rf = np.round(lut[1, :3] * 255).astype(int)
    if tuple(rf) != (0x7F, 0x00, 0xCF):
        raise ValueError(f"index 1 must be RF purple #7F00CF, got {rf}")
    return lut


def build(source: str, out: Path) -> Path:
    if re.match(r"^https?://", source):
        with urllib.request.urlopen(source, timeout=60) as resp:  # noqa: S310
            text = resp.read().decode("utf-8", "replace")
    else:
        text = Path(source).read_text()
    lut = parse_cmap(text)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, rgba_float=lut)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="build_awips_vel_lut", description=__doc__)
    ap.add_argument("--source", default=DEFAULT_SOURCE,
                    help="URL or local path of the AWIPS .cmap XML")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="output .npz path")
    args = ap.parse_args(argv)
    out = build(args.source, Path(args.out))
    lut = np.load(out)["rgba_float"]
    print(f"wrote {out}  rgba_float{lut.shape}  "
          f"rf=#{''.join(f'{int(round(v * 255)):02X}' for v in lut[1, :3])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
