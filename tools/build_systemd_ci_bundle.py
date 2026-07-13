#!/usr/bin/env python3
"""Build an ephemeral signed source bundle for the Bookworm systemd fixture."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from pathlib import Path

if __package__:
    from tools.build_install_bundle import _write_inner_manifest, sign_manifest
else:
    from build_install_bundle import _write_inner_manifest, sign_manifest


SOURCE_DIRECTORIES = ("src", "systemd", "config", "scripts", "constraints")
SOURCE_FILES = ("pyproject.toml", "README.md", "MANIFEST.in", "LICENSE")


def build_fixture_bundle(
    source: Path,
    output: Path,
    version: str,
    signing_key: Path,
    *,
    failing_service: bool = False,
) -> Path:
    """Create a complete signed fixture without changing the source checkout."""

    if output.exists() or output.is_symlink():
        raise ValueError(f"fixture output already exists: {output}")
    output.mkdir(mode=0o700, parents=True)
    for name in SOURCE_DIRECTORIES:
        shutil.copytree(source / name, output / name, symlinks=False)
    for name in SOURCE_FILES:
        shutil.copy2(source / name, output / name)

    environment = dict(os.environ)
    environment["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RETICULUMPI"] = version
    wheel_directory = output / ".fixture-wheel"
    wheel_directory.mkdir(mode=0o700)
    subprocess.run(
        [
            "python",
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(wheel_directory),
            str(output),
        ],
        check=True,
        env=environment,
    )
    wheels = list(wheel_directory.glob("reticulumpi-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("fixture build did not produce exactly one wheel")
    wheel = output / wheels[0].name
    shutil.move(wheels[0], wheel)
    wheel_directory.rmdir()

    # The administrator reads generated version metadata without importing it.
    (output / "src/reticulumpi/_version.py").write_text(
        f'__version__ = "{version}"\n', encoding="utf-8"
    )
    if failing_service:
        service = output / "systemd/reticulumpi.service"
        rendered = service.read_text(encoding="utf-8")
        rendered = rendered.replace(
            "ExecStart=/opt/reticulumpi/current/.venv/bin/reticulumpi "
            "--config /etc/reticulumpi/config.yaml",
            "ExecStart=/bin/false",
        )
        if "ExecStart=/bin/false" not in rendered:
            raise RuntimeError("could not inject the systemd readiness failure")
        service.write_text(rendered, encoding="utf-8")

    (output / "bundle.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "kind": "reticulumpi-install",
                "version": version,
                "architecture": "arm64",
                "wheel": wheel.name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = _write_inner_manifest(output)
    sign_manifest(manifest, output / "SHA256SUMS.minisig", signing_key, Path("/usr/bin/minisign"))
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--signing-key", required=True, type=Path)
    parser.add_argument("--failing-service", action="store_true")
    args = parser.parse_args()
    print(
        build_fixture_bundle(
            args.source.resolve(),
            args.output.resolve(),
            args.version,
            args.signing_key.resolve(),
            failing_service=args.failing_service,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
