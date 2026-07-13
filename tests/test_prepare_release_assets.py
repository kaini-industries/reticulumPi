"""Focused release-input and provenance validation tests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tools import prepare_release_assets


def _write_provenance(directory: Path, document: dict[str, object]) -> Path:
    path = directory / prepare_release_assets.RELEASE_PROVENANCE_NAME
    path.write_text(
        json.dumps(document, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="ascii",
    )
    return path


def test_release_provenance_accepts_only_the_canonical_regular_file(tmp_path: Path) -> None:
    provenance = _write_provenance(
        tmp_path,
        {"commit": "a" * 40, "run_id": 123, "schema": 1, "tag": "v3.2.1"},
    )

    assert prepare_release_assets._validate_release_provenance(provenance) == provenance

    link = tmp_path / "link" / prepare_release_assets.RELEASE_PROVENANCE_NAME
    link.parent.mkdir()
    link.symlink_to(provenance)
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="escapes|regular file"):
        prepare_release_assets._validate_release_provenance(link)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b'{"tag":"v3.2.1", "schema":1}\n', "canonical"),
        (b'{"tag":"v3.2.1","schema":1}\n', "canonical"),
        (b'{"schema":1,"schema":1}\n', "canonical"),
        (b'["not-an-object"]\n', "root must be an object"),
        (b'{"schema":NaN}\n', "not valid JSON"),
        (b"{broken}\n", "not valid JSON"),
    ),
)
def test_release_provenance_rejects_noncanonical_json(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    provenance = tmp_path / prepare_release_assets.RELEASE_PROVENANCE_NAME
    provenance.write_bytes(payload)

    with pytest.raises(prepare_release_assets.ReleaseAssetError, match=message):
        prepare_release_assets._validate_release_provenance(provenance)


def test_release_provenance_rejects_wrong_name_and_oversize(tmp_path: Path) -> None:
    wrong_name = tmp_path / "provenance.json"
    wrong_name.write_text("{}\n", encoding="ascii")
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="must be named"):
        prepare_release_assets._validate_release_provenance(wrong_name)

    oversized = tmp_path / prepare_release_assets.RELEASE_PROVENANCE_NAME
    oversized.write_bytes(b"{" + b" " * prepare_release_assets.MAX_RELEASE_PROVENANCE_BYTES + b"}")
    with pytest.raises(prepare_release_assets.ReleaseAssetError, match="exceeds"):
        prepare_release_assets._validate_release_provenance(oversized)


def test_validated_release_inputs_is_immutable(tmp_path: Path) -> None:
    inputs = prepare_release_assets.ValidatedReleaseInputs(
        version="3.2.1",
        wheel=tmp_path / "release.whl",
        sdist=tmp_path / "release.tar.gz",
        sbom=tmp_path / "release.cdx.json",
        amd64_image=tmp_path / "amd64.tar.gz",
        arm64_image=tmp_path / "arm64.tar.gz",
        recovery_artifacts=(),
        provenance=tmp_path / prepare_release_assets.RELEASE_PROVENANCE_NAME,
    )

    with pytest.raises(FrozenInstanceError):
        inputs.version = "3.2.2"
