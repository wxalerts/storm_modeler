"""GUI smoke test (Section 8B).

Runs the offscreen ``--smoke`` build in a subprocess and asserts a clean exit.
A subprocess is used deliberately: VTK's offscreen GL context can abort during
*interpreter* teardown on some headless stacks, which is cosmetic but would
otherwise poison an in-process pytest run. ``--smoke`` itself drives a fixture
through every pane, so a non-zero exit means a real wiring regression.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_offscreen_smoke_exits_clean():
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-m", "storm_modeler.app", "--smoke"],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"--smoke exited {proc.returncode}\nstdout:\n{proc.stdout}\n"
        f"stderr tail:\n{proc.stderr[-2000:]}"
    )
    assert "OK" in proc.stdout
