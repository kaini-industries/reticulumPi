#!/usr/bin/env python3
"""Execute explicitly tagged documentation shell examples in a clean environment."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "SECURITY.md",
    *sorted((ROOT / "docs").glob("*.md")),
)
TAG = "bookworm-doctest"


@dataclass(frozen=True)
class ShellExample:
    path: Path
    line: int
    script: str


def discover_examples() -> list[ShellExample]:
    examples: list[ShellExample] = []
    for path in DOCUMENTS:
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 0
        while index < len(lines):
            info = lines[index].strip().split()
            if not info or info[0] not in {"```bash", "```sh", "```shell"} or TAG not in info[1:]:
                index += 1
                continue
            start = index + 2
            index += 1
            body: list[str] = []
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            if index >= len(lines):
                raise RuntimeError(f"unterminated {TAG} fence at {path.relative_to(ROOT)}:{start}")
            script = "\n".join(body).strip()
            if not script:
                raise RuntimeError(f"empty {TAG} fence at {path.relative_to(ROOT)}:{start}")
            examples.append(ShellExample(path, start, script))
            index += 1
    if not examples:
        raise RuntimeError(f"no documentation fences are tagged {TAG}")
    return examples


def _require_bookworm() -> None:
    values: dict[str, str] = {}
    for raw_line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        values[key] = value.strip().strip('"')
    if values.get("VERSION_CODENAME") != "bookworm" or sys.version_info[:2] != (3, 11):
        raise RuntimeError("documentation shell examples require Bookworm and Python 3.11")


def run_examples(examples: list[ShellExample]) -> None:
    with tempfile.TemporaryDirectory(prefix="reticulumpi-doc-examples-") as home:
        environment = {
            "HOME": home,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": home,
        }
        for example in examples:
            label = f"{example.path.relative_to(ROOT)}:{example.line}"
            print(f"Running documentation shell example {label}")
            subprocess.run(
                ["/bin/bash", "-euo", "pipefail", "-c", example.script],
                cwd=ROOT,
                env=environment,
                check=True,
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list examples without executing")
    parser.add_argument(
        "--require-bookworm",
        action="store_true",
        help="fail unless running on Bookworm with Python 3.11",
    )
    arguments = parser.parse_args()
    examples = discover_examples()
    if arguments.list:
        for example in examples:
            print(f"{example.path.relative_to(ROOT)}:{example.line}")
        return 0
    if arguments.require_bookworm:
        _require_bookworm()
    run_examples(examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
