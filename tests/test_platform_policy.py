"""Complete-tuple tests for the production platform policy."""

from __future__ import annotations

from dataclasses import fields

import pytest

from reticulumpi.platform_policy import (
    LEGACY_UNIVERSAL_DEPENDENCY_PROFILES,
    LEGACY_UNIVERSAL_HASH_LOCK_SET,
    PlatformProfile,
    UNIVERSAL_DEPENDENCY_PROFILES,
    UnsupportedPlatformError,
    normalise_architecture,
    select_platform_profile,
)


@pytest.mark.parametrize("distribution", ["debian", "raspbian"])
@pytest.mark.parametrize("machine", ["aarch64", "arm64"])
def test_bookworm_py311_lane_has_stable_profile_and_metadata(distribution, machine):
    profile = select_platform_profile(
        system="Linux",
        machine=machine,
        version_info=(3, 11, 9),
        os_release={
            "ID": distribution,
            "VERSION_CODENAME": "bookworm",
            "VERSION_ID": "12",
        },
    )

    assert profile.key == "linux-arm64-debian-bookworm-py311"
    assert profile.profile_key == profile.key
    assert profile.distribution == distribution
    assert profile.python == "3.11.9"
    assert profile.python_series == "3.11"
    assert profile.dependency_lock_scope == "shared-universal"
    assert profile.dependency_profiles == UNIVERSAL_DEPENDENCY_PROFILES
    assert profile.as_metadata() == {
        "profile_key": "linux-arm64-debian-bookworm-py311",
        "system": "Linux",
        "architecture": "arm64",
        "distribution": distribution,
        "codename": "bookworm",
        "version_id": "12",
        "python": "3.11.9",
        "python_series": "3.11",
        "dependency_lock_set": "production-universal-v1",
        "dependency_lock_scope": "shared-universal",
        "dependency_profiles": {
            "core": "production-universal-core.txt",
            "dashboard-nomadnet": "production-universal-dashboard-nomadnet.txt",
            "all-features": "production-universal-all-features.txt",
        },
    }


def test_bookworm_version_id_may_be_absent_but_is_canonicalized():
    profile = select_platform_profile(
        system="linux",
        machine="AARCH64",
        version_info=(3, 11),
        os_release={"ID": "Debian", "VERSION_CODENAME": "Bookworm"},
    )

    assert profile.version_id == "12"
    assert profile.python == "3.11"


def test_noble_py312_lane_has_distinct_stable_profile_and_shared_locks():
    profile = select_platform_profile(
        system="Linux",
        machine="aarch64",
        version_info=(3, 12, 4),
        os_release={
            "ID": "ubuntu",
            "VERSION_CODENAME": "noble",
            "VERSION_ID": "24.04",
        },
    )

    assert profile.key == "linux-arm64-ubuntu-noble-py312"
    assert profile.python == "3.12.4"
    assert profile.python_series == "3.12"
    assert profile.dependency_lock_set == "production-universal-v1"
    assert profile.dependency_profiles is UNIVERSAL_DEPENDENCY_PROFILES


def test_legacy_dependency_names_are_disjoint_read_compatibility_metadata():
    assert LEGACY_UNIVERSAL_HASH_LOCK_SET == "bookworm-py311-universal"
    assert LEGACY_UNIVERSAL_DEPENDENCY_PROFILES.keys() == UNIVERSAL_DEPENDENCY_PROFILES.keys()
    assert set(LEGACY_UNIVERSAL_DEPENDENCY_PROFILES.values()).isdisjoint(
        UNIVERSAL_DEPENDENCY_PROFILES.values()
    )
    assert all(
        filename.startswith("bookworm-py311-")
        for filename in LEGACY_UNIVERSAL_DEPENDENCY_PROFILES.values()
    )


def test_dependency_profile_default_uses_a_python311_compatible_factory():
    dependency_profiles = next(
        item for item in fields(PlatformProfile) if item.name == "dependency_profiles"
    )
    assert dependency_profiles.default_factory() is UNIVERSAL_DEPENDENCY_PROFILES


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"system": "Darwin"},
            "unsupported operating system: expected Linux, found Darwin",
        ),
        (
            {"machine": "x86_64"},
            "unsupported architecture: expected ARM64, found amd64",
        ),
        (
            {"os_release": {"ID": "debian", "VERSION_CODENAME": "trixie"}},
            "unsupported Debian/Raspbian release: expected Bookworm 12, found trixie unknown",
        ),
        (
            {
                "os_release": {
                    "ID": "raspbian",
                    "VERSION_CODENAME": "bookworm",
                    "VERSION_ID": "11",
                }
            },
            "unsupported Debian/Raspbian release: expected Bookworm 12, found bookworm 11",
        ),
        (
            {"version_info": (3, 12, 0)},
            "unsupported Python for Debian/Raspbian Bookworm: expected 3.11, found 3.12.0",
        ),
        (
            {"os_release": {"ID": "fedora", "VERSION_ID": "42"}},
            "unsupported Linux ARM64 distribution: expected Debian/Raspbian Bookworm "
            "or Ubuntu Noble 24.04, found fedora unknown 42",
        ),
    ],
)
def test_other_bookworm_lane_tuples_are_rejected_precisely(overrides, message):
    inputs = {
        "system": "Linux",
        "machine": "arm64",
        "version_info": (3, 11, 9),
        "os_release": {
            "ID": "debian",
            "VERSION_CODENAME": "bookworm",
            "VERSION_ID": "12",
        },
    }
    inputs.update(overrides)

    with pytest.raises(UnsupportedPlatformError) as raised:
        select_platform_profile(**inputs)
    assert str(raised.value) == message


@pytest.mark.parametrize(
    ("os_release", "version_info", "message"),
    [
        (
            {"ID": "ubuntu", "VERSION_CODENAME": "jammy", "VERSION_ID": "22.04"},
            (3, 12, 1),
            "unsupported Ubuntu release: expected Noble 24.04, found jammy 22.04",
        ),
        (
            {"ID": "ubuntu", "VERSION_CODENAME": "noble", "VERSION_ID": "22.04"},
            (3, 12, 1),
            "unsupported Ubuntu release: expected Noble 24.04, found noble 22.04",
        ),
        (
            {"ID": "ubuntu", "VERSION_CODENAME": "noble", "VERSION_ID": "24.04"},
            (3, 11, 9),
            "unsupported Python for Ubuntu Noble 24.04: expected 3.12, found 3.11.9",
        ),
    ],
)
def test_other_ubuntu_lane_tuples_are_rejected_precisely(os_release, version_info, message):
    with pytest.raises(UnsupportedPlatformError) as raised:
        select_platform_profile(
            system="Linux",
            machine="arm64",
            version_info=version_info,
            os_release=os_release,
        )
    assert str(raised.value) == message


def test_python_version_shape_fails_with_lane_specific_message():
    with pytest.raises(UnsupportedPlatformError) as raised:
        select_platform_profile(
            system="Linux",
            machine="arm64",
            version_info=(3,),
            os_release={
                "ID": "ubuntu",
                "VERSION_CODENAME": "noble",
                "VERSION_ID": "24.04",
            },
        )
    assert str(raised.value) == (
        "unsupported Python for Ubuntu Noble 24.04: expected 3.12, found 3"
    )


def test_architecture_normalization_does_not_expand_supported_architectures():
    assert normalise_architecture(" aarch64 ") == "arm64"
    assert normalise_architecture("AMD64") == "amd64"
    assert normalise_architecture("armv7l") == "armv7l"
