"""Path helpers for locating repo-relative assets from installed plugins."""

from __future__ import annotations

import os
import sys


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
    candidates.append(
        os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
    )
    # Standard system install
    candidates.append("/opt/reticulumpi")
    for root in candidates:
        path = os.path.join(root, *parts)
        if os.path.exists(path):
            return path
    return None
