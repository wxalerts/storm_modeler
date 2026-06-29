"""Logging + crash diagnostics.

Centralises structlog configuration so every run writes to **both** stderr and a
timestamped file under ``.cache/logs/``, and installs :mod:`faulthandler` so a
native crash (segfault/abort — e.g. a flaky GL driver) dumps the Python stack of
all threads into the log instead of vanishing silently. This is what makes the
hard-to-reproduce VTK/Qt crashes on some display stacks debuggable after the
fact.

Call :func:`setup_logging` once at process start (the CLI entry does). Use
:func:`log_gl_info` after the first VTK render window exists to record the
OpenGL vendor/renderer/version — the first thing to check when a render crashes.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .config import CACHE_DIR

log = structlog.get_logger(__name__)

_LOG_FH = None  # keep the file handle alive for faulthandler's lifetime
_LOG_PATH: Path | None = None

_LEVELS = {
    "debug": logging.DEBUG, "info": logging.INFO,
    "warning": logging.WARNING, "error": logging.ERROR,
}


def log_dir() -> Path:
    return Path(os.environ.get("STORM_MODELER_LOG_DIR", CACHE_DIR / "logs"))


def current_log_path() -> Path | None:
    return _LOG_PATH


class _Tee:
    """Write structlog output to stdout and the log file at once.

    stdout (not stderr) so every log line — including errors — pipes out where
    the dev is watching and to anything consuming the process's stdout.
    """

    def __init__(self, fh) -> None:
        self._fh = fh

    def write(self, s: str) -> int:
        sys.stdout.write(s)
        try:
            self._fh.write(s)
        except Exception:  # noqa: BLE001
            pass
        return len(s)

    def flush(self) -> None:
        try:
            sys.stdout.flush()
            self._fh.flush()
        except Exception:  # noqa: BLE001
            pass


def setup_logging(level: str | None = None) -> Path:
    """Configure structlog (stderr + file) and crash dumps; return the log path."""
    global _LOG_FH, _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH

    level = (level or os.environ.get("STORM_MODELER_LOG_LEVEL", "info")).lower()
    lvl = _LEVELS.get(level, logging.INFO)

    d = log_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = d / f"storm_modeler_{stamp}_{os.getpid()}.log"
    fh = open(path, "a", buffering=1, encoding="utf-8")
    _LOG_FH = fh
    _LOG_PATH = path

    # Native-crash diagnostics: dump every thread's Python stack on
    # SIGSEGV/SIGABRT/SIGBUS/SIGFPE — once into the log file (survives the
    # terminal closing) and once to stdout (where the dev is watching, so a
    # segfault is no longer silently swallowed). faulthandler needs a real file
    # descriptor; the last enable() wins, so register the file first and stdout
    # last to keep both targets active.
    for target in (fh, sys.stdout):
        try:
            faulthandler.enable(file=target, all_threads=True)
        except Exception:  # noqa: BLE001
            pass

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.WriteLoggerFactory(file=_Tee(fh)),
        cache_logger_on_first_use=False,
    )

    # Route uncaught *Python* exceptions through the logger (→ stdout + file)
    # instead of the interpreter's default stderr dump, which Qt can also
    # swallow when an exception escapes a slot. Covers the main thread and
    # QThreadPool/threading workers.
    def _log_uncaught(exc_type, exc, tb) -> None:
        log.error("uncaught", exc_info=(exc_type, exc, tb))

    def _log_uncaught_thread(args) -> None:
        log.error(
            "uncaught.thread", thread=getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _log_uncaught
    threading.excepthook = _log_uncaught_thread

    log.info(
        "logging.start", path=str(path), pid=os.getpid(), level=level,
        display=os.environ.get("DISPLAY"),
        qt_platform=os.environ.get("QT_QPA_PLATFORM"),
        wayland=os.environ.get("WAYLAND_DISPLAY"),
    )
    return path


def log_gl_info(plotter, where: str = "map") -> None:
    """Record the OpenGL vendor/renderer/version of a plotter's render window."""
    try:
        caps = plotter.ren_win.ReportCapabilities()
    except Exception as e:  # noqa: BLE001
        log.info("gl.context_unavailable", where=where, reason=str(e).splitlines()[0])
        return
    fields = {}
    for key in ("OpenGL vendor string", "OpenGL renderer string",
                "OpenGL version string"):
        for line in caps.splitlines():
            if line.strip().startswith(key):
                fields[key.split()[1]] = line.split(":", 1)[-1].strip()
                break
    log.info("gl.context", where=where, **fields)
