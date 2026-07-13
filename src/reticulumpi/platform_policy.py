"""Supported production platform tuples and their release metadata.

Platform support is deliberately expressed as complete tuples.  Allowing each
component independently would, for example, accidentally admit Bookworm with
Python 3.12 or Noble with Python 3.11 even though neither is a supported lane.

The current dependency locks were generated with uv's ``--universal`` mode.
Both supported lanes intentionally select the same canonical
``production-universal`` files.  The legacy filename map exists only so a new
administrator can read already signed bundles and persisted installation
metadata created before the canonical rename.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence


class UnsupportedPlatformError(ValueError):
    """Raised when a host does not match a complete supported platform tuple."""


UNIVERSAL_DEPENDENCY_PROFILES: Mapping[str, str] = MappingProxyType(
    {
        "core": "production-universal-core.txt",
        "dashboard-nomadnet": "production-universal-dashboard-nomadnet.txt",
        "all-features": "production-universal-all-features.txt",
    }
)
"""Shared universal hash-lock filenames used by every supported platform lane."""

LEGACY_UNIVERSAL_DEPENDENCY_PROFILES: Mapping[str, str] = MappingProxyType(
    {
        "core": "bookworm-py311-core.txt",
        "dashboard-nomadnet": "bookworm-py311-dashboard-nomadnet.txt",
        "all-features": "bookworm-py311-all-features.txt",
    }
)
"""Read-only aliases used by previously signed release bundles."""

UNIVERSAL_HASH_LOCK_SET = "production-universal-v1"
LEGACY_UNIVERSAL_HASH_LOCK_SET = "bookworm-py311-universal"


@dataclass(frozen=True)
class PlatformProfile:
    """Canonical policy result for one validated production host."""

    key: str
    system: str
    architecture: str
    distribution: str
    codename: str
    version_id: str
    python: str
    python_series: str
    dependency_lock_set: str = UNIVERSAL_HASH_LOCK_SET
    dependency_lock_scope: str = "shared-universal"
    dependency_profiles: Mapping[str, str] = field(
        default_factory=lambda: UNIVERSAL_DEPENDENCY_PROFILES
    )

    @property
    def profile_key(self) -> str:
        """Return the stable key under a descriptive alias for callers."""

        return self.key

    def as_metadata(self) -> dict[str, object]:
        """Return JSON-serializable installation metadata."""

        return {
            "profile_key": self.key,
            "system": self.system,
            "architecture": self.architecture,
            "distribution": self.distribution,
            "codename": self.codename,
            "version_id": self.version_id,
            "python": self.python,
            "python_series": self.python_series,
            "dependency_lock_set": self.dependency_lock_set,
            "dependency_lock_scope": self.dependency_lock_scope,
            "dependency_profiles": dict(self.dependency_profiles),
        }


def normalise_architecture(machine: str) -> str:
    """Return the release-bundle architecture name for common host aliases."""

    value = machine.strip().lower()
    if value in {"aarch64", "arm64"}:
        return "arm64"
    if value in {"x86_64", "amd64"}:
        return "amd64"
    return value


def _render_found(*parts: str) -> str:
    return " ".join(part or "unknown" for part in parts)


def _python_parts(version_info: Sequence[int]) -> tuple[tuple[int, int] | None, str]:
    try:
        parts = tuple(int(part) for part in version_info[:3])
    except (TypeError, ValueError):
        return None, "unknown"
    if len(parts) < 2 or any(part < 0 for part in parts):
        return None, ".".join(str(part) for part in parts) or "unknown"
    return (parts[0], parts[1]), ".".join(str(part) for part in parts)


def select_platform_profile(
    *,
    system: str,
    machine: str,
    version_info: Sequence[int],
    os_release: Mapping[str, str],
) -> PlatformProfile:
    """Validate a complete host tuple and return its canonical profile.

    Supported lanes are Linux ARM64 Debian/Raspbian Bookworm on Python 3.11
    and Linux ARM64 Ubuntu Noble 24.04 on Python 3.12.
    """

    live_system = system.strip()
    if live_system.lower() != "linux":
        raise UnsupportedPlatformError(
            f"unsupported operating system: expected Linux, found {live_system or 'unknown'}"
        )

    architecture = normalise_architecture(machine)
    if architecture != "arm64":
        raise UnsupportedPlatformError(
            f"unsupported architecture: expected ARM64, found {architecture or 'unknown'}"
        )

    distribution = os_release.get("ID", "").strip().lower()
    codename = os_release.get("VERSION_CODENAME", "").strip().lower()
    version_id = os_release.get("VERSION_ID", "").strip()
    python_series, rendered_python = _python_parts(version_info)

    if distribution in {"debian", "raspbian"}:
        if codename != "bookworm" or version_id not in {"", "12"}:
            raise UnsupportedPlatformError(
                "unsupported Debian/Raspbian release: expected Bookworm 12, found "
                f"{_render_found(codename, version_id)}"
            )
        if python_series != (3, 11):
            raise UnsupportedPlatformError(
                "unsupported Python for Debian/Raspbian Bookworm: expected 3.11, "
                f"found {rendered_python}"
            )
        return PlatformProfile(
            key="linux-arm64-debian-bookworm-py311",
            system="Linux",
            architecture="arm64",
            distribution=distribution,
            codename="bookworm",
            version_id="12",
            python=rendered_python,
            python_series="3.11",
        )

    if distribution == "ubuntu":
        if codename != "noble" or version_id != "24.04":
            raise UnsupportedPlatformError(
                "unsupported Ubuntu release: expected Noble 24.04, found "
                f"{_render_found(codename, version_id)}"
            )
        if python_series != (3, 12):
            raise UnsupportedPlatformError(
                f"unsupported Python for Ubuntu Noble 24.04: expected 3.12, found {rendered_python}"
            )
        return PlatformProfile(
            key="linux-arm64-ubuntu-noble-py312",
            system="Linux",
            architecture="arm64",
            distribution="ubuntu",
            codename="noble",
            version_id="24.04",
            python=rendered_python,
            python_series="3.12",
        )

    raise UnsupportedPlatformError(
        "unsupported Linux ARM64 distribution: expected Debian/Raspbian Bookworm "
        "or Ubuntu Noble 24.04, found "
        f"{_render_found(distribution, codename, version_id)}"
    )
