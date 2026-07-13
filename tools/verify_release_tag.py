#!/usr/bin/env python3
"""Validate release tag policy and artifact versions.

This tool deliberately separates structural signature enforcement from trust
verification.  ``--require-signature`` rejects lightweight and unsigned tags;
``--verify-signature`` additionally delegates cryptographic verification to
Git, using the release keyring configured on the host.
"""

from __future__ import annotations

import argparse
import email.parser
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path


RELEASE_TAG = re.compile(r"^v(?P<version>0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SIGNATURE_MARKERS = (
    "-----BEGIN PGP SIGNATURE-----",
    "-----BEGIN SSH SIGNATURE-----",
)
OPENPGP_FINGERPRINT = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")


def version_from_tag(tag: str) -> str:
    """Return the PEP 440 release version encoded by a strict release tag."""
    match = RELEASE_TAG.fullmatch(tag)
    if match is None:
        raise ValueError(f"release tag must be vMAJOR.MINOR.PATCH, got {tag!r}")
    return tag[1:]


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def verify_tag_object(
    tag: str,
    *,
    require_signature: bool,
    verify_signature: bool,
    trusted_key: Path | None = None,
    trusted_fingerprint: str | None = None,
) -> None:
    """Validate that *tag* is annotated, signed, and optionally trusted."""
    if (trusted_key is None) != (trusted_fingerprint is None):
        raise ValueError("trusted key and fingerprint must be supplied together")
    if trusted_key is not None and not verify_signature:
        raise ValueError("a trusted key requires cryptographic signature verification")
    object_type = _git("cat-file", "-t", f"refs/tags/{tag}").strip()
    if object_type != "tag":
        raise ValueError(f"release tag {tag!r} must be an annotated tag")
    tagged_commit = _git("rev-parse", f"refs/tags/{tag}^{{}}").strip()
    current_commit = _git("rev-parse", "HEAD").strip()
    if tagged_commit != current_commit:
        raise ValueError(f"release tag {tag!r} does not identify the checked-out commit")

    if require_signature or verify_signature:
        tag_object = _git("cat-file", "-p", f"refs/tags/{tag}")
        if not any(marker in tag_object for marker in SIGNATURE_MARKERS):
            raise ValueError(f"release tag {tag!r} is not signed")

    if verify_signature:
        if trusted_key is None:
            subprocess.run(["git", "verify-tag", tag], check=True)
        else:
            _verify_with_trusted_openpgp_key(tag, trusted_key, trusted_fingerprint or "")


def _verify_with_trusted_openpgp_key(tag: str, key: Path, fingerprint: str) -> None:
    """Verify *tag* in an isolated keyring and require the configured signer."""

    expected = re.sub(r"\s+", "", fingerprint).upper()
    if OPENPGP_FINGERPRINT.fullmatch(expected) is None:
        raise ValueError("trusted OpenPGP fingerprint must contain 40 or 64 hexadecimal digits")
    try:
        metadata = key.lstat()
    except OSError as exc:
        raise ValueError(f"trusted release tag key is unavailable: {key}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"trusted release tag key must be a regular file: {key}")

    with tempfile.TemporaryDirectory(prefix="reticulumpi-tag-keyring-") as raw:
        keyring = Path(raw)
        keyring.chmod(0o700)
        environment = os.environ.copy()
        environment.update({"GNUPGHOME": str(keyring), "LC_ALL": "C"})
        show = subprocess.run(
            [
                "gpg",
                "--batch",
                "--with-colons",
                "--import-options",
                "show-only",
                "--import",
                str(key),
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        key_fingerprints = {
            fields[9].upper()
            for line in show.stdout.splitlines()
            if (fields := line.split(":"))[0] == "fpr" and len(fields) > 9
        }
        if expected not in key_fingerprints:
            raise ValueError("configured release tag fingerprint is absent from the trusted key")
        subprocess.run(
            ["gpg", "--batch", "--import", str(key)],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        verified = subprocess.run(
            [
                "git",
                "-c",
                "gpg.format=openpgp",
                "-c",
                "gpg.program=gpg",
                "verify-tag",
                "--raw",
                tag,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
    status = f"{verified.stdout}\n{verified.stderr}".upper()
    valid_signers = {
        field
        for line in status.splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
        for field in line.split()[2:]
        if OPENPGP_FINGERPRINT.fullmatch(field)
    }
    if expected not in valid_signers:
        raise ValueError("release tag signature is valid but not from the configured signer")


def _metadata_version(raw: bytes, artifact: Path) -> str:
    message = email.parser.BytesParser().parsebytes(raw)
    distribution = message.get("Name", "")
    normalized_distribution = re.sub(r"[-_.]+", "-", distribution).lower()
    if normalized_distribution != "reticulumpi":
        raise ValueError(f"artifact metadata is not for ReticulumPi: {artifact}")
    version = message.get("Version")
    if not version:
        raise ValueError(f"artifact metadata has no Version field: {artifact}")
    return version


def artifact_version(artifact: Path) -> str:
    """Read the distribution version from wheel or sdist metadata."""
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise ValueError(f"wheel must contain exactly one METADATA file: {artifact}")
            return _metadata_version(archive.read(metadata_names[0]), artifact)

    if artifact.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        with tarfile.open(artifact, mode="r:*") as archive:
            metadata = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.count("/") == 1
                and member.name.endswith("/PKG-INFO")
            ]
            if len(metadata) != 1:
                raise ValueError(f"sdist must contain exactly one top-level PKG-INFO: {artifact}")
            extracted = archive.extractfile(metadata[0])
            if extracted is None:
                raise ValueError(f"could not read sdist metadata: {artifact}")
            return _metadata_version(extracted.read(), artifact)

    raise ValueError(f"unsupported release artifact: {artifact}")


def verify_artifacts(artifacts: list[Path], expected_version: str) -> None:
    if not artifacts:
        raise ValueError("at least one wheel or sdist is required")
    for artifact in artifacts:
        actual_version = artifact_version(artifact)
        if actual_version != expected_version:
            raise ValueError(
                f"artifact version mismatch for {artifact}: "
                f"expected {expected_version}, got {actual_version}"
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify ReticulumPi release tag and artifact version policy.",
    )
    parser.add_argument("tag", help="release tag in strict vMAJOR.MINOR.PATCH form")
    parser.add_argument("artifacts", type=Path, nargs="+", help="wheel and sdist artifacts")
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="require an annotated tag object containing a Git signature",
    )
    parser.add_argument(
        "--verify-signature",
        action="store_true",
        help="also run git verify-tag using the configured trusted keyring",
    )
    parser.add_argument(
        "--trusted-key",
        type=Path,
        help="verify with this OpenPGP public key in an isolated keyring",
    )
    parser.add_argument(
        "--trusted-fingerprint",
        help="required full fingerprint for --trusted-key",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    expected_version = version_from_tag(args.tag)
    verify_tag_object(
        args.tag,
        require_signature=args.require_signature,
        verify_signature=args.verify_signature,
        trusted_key=args.trusted_key,
        trusted_fingerprint=args.trusted_fingerprint,
    )
    verify_artifacts(args.artifacts, expected_version)
    print(f"Verified release tag {args.tag} and {len(args.artifacts)} artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
