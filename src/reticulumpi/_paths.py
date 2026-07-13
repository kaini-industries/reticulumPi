"""Path helpers for locating repo-relative assets from installed plugins."""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, files
from pathlib import Path, PurePosixPath


PRODUCTION_STATE_ROOT = Path("/var/lib/reticulumpi")
PRODUCTION_CACHE_ROOT = Path("/var/cache/reticulumpi")


def runtime_state_path(*parts: str) -> str:
    """Return a path below the service-owned durable-state root."""

    root = Path(os.environ.get("RETICULUMPI_STATE_DIR", PRODUCTION_STATE_ROOT))
    return str(root.joinpath(*parts))


def runtime_cache_path(*parts: str) -> str:
    """Return a path below the already application-scoped cache root.

    The production unit sets ``XDG_CACHE_HOME=/var/cache/reticulumpi`` and
    the container sets it to ``/cache``. Do not append the application name
    again or those supported layouts would gain an extra nested directory.
    """

    root = Path(os.environ.get("XDG_CACHE_HOME", PRODUCTION_CACHE_ROOT))
    return str(root.joinpath(*parts))


def find_distribution_asset(*parts: str) -> str | None:
    """Locate a wheel data file installed under ``share/reticulumpi``.

    ``setuptools.data-files`` entries are relocated to the active environment
    when a wheel is installed.  Querying distribution metadata avoids assuming
    a particular virtualenv or system prefix.
    """

    suffix = ("share", "reticulumpi", *parts)
    try:
        entries = files("reticulumpi")
    except PackageNotFoundError:
        return None
    for entry in entries or ():
        entry_parts = PurePosixPath(str(entry)).parts
        for start in range(0, len(entry_parts) - len(suffix) + 1):
            if entry_parts[start : start + len(suffix)] != suffix:
                continue
            candidate = Path(entry.locate()).resolve()
            trailing_parts = len(entry_parts) - start - len(suffix)
            for _ in range(trailing_parts):
                candidate = candidate.parent
            if candidate.exists():
                return str(candidate)
    return None


def find_repo_asset(*parts: str) -> str | None:
    """Return the absolute path of a repo asset (scripts/, config/, ...).

    `__file__` resolves inside site-packages when reticulumpi is installed
    from a wheel or copied there by update.sh, so a naive dirname walk
    lands in `.../lib/python3.12`, not the repo root. Try several
    candidates and return the first that exists.
    """
    candidates: list[str] = []
    env_root = os.environ.get("RETICULUMPI_ROOT")
    if env_root:
        candidates.append(env_root)
    # Editable install: the .pth file adds `<repo>/src` to sys.path
    for entry in sys.path:
        if entry.endswith("/src") and os.path.isdir(os.path.join(entry, "reticulumpi")):
            candidates.append(os.path.dirname(entry))
    # Editable install where __file__ itself is in the repo tree
    # (`<repo>/src/reticulumpi/_paths.py` → <repo>)
    candidates.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    # Standard system install
    candidates.append("/opt/reticulumpi")
    for root in candidates:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    return None
