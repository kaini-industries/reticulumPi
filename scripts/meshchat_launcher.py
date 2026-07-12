#!/usr/bin/env python3
"""Source-checkout compatibility launcher for the packaged MeshChat wrapper."""

from __future__ import annotations

import runpy
from pathlib import Path


if __name__ == "__main__":
    launcher = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "reticulumpi"
        / "data"
        / "meshchat_launcher.pydata"
    )
    runpy.run_path(str(launcher), run_name="__main__")
