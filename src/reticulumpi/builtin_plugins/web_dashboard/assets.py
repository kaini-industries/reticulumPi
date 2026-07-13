"""Validated access to packaged dashboard resources and build metadata."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Mapping


_PACKAGE = "reticulumpi.builtin_plugins.web_dashboard"
_ASSET_PLACEHOLDER = re.compile(r"\{\{ASSET:([a-z0-9.-]+)}}")
_FEATURE_PLACEHOLDER = re.compile(r"\{\{FEATURE:([a-z0-9-]+)}}")
_READY_FEATURES_PLACEHOLDER = "{{READY_FEATURES}}"
_KNOWN_FEATURES = frozenset(
    {
        "acars",
        "adsb",
        "ais",
        "gps",
        "hotspot",
        "link-tester",
        "map",
        "mesh",
        "mesh-bridge",
        "meshcore",
        "messages",
        "meshtastic",
        "noaa",
        "ntp",
        "radio",
        "radiosonde",
        "routing",
        "space",
        "weather-alert",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_ASSETS = frozenset(
    {
        "dashboard.css",
        "dashboard.js",
        "feature-adsb.js",
        "feature-acars.js",
        "feature-ais.js",
        "feature-gps.js",
        "feature-hotspot.js",
        "feature-link-tester.js",
        "feature-lora.js",
        "feature-map.js",
        "feature-mesh-bridge.js",
        "feature-mesh.js",
        "feature-meshcore.js",
        "feature-messages.js",
        "feature-meshtastic.js",
        "feature-noaa.js",
        "feature-ntp.js",
        "feature-radio.js",
        "feature-radiosonde.js",
        "feature-routing.js",
        "feature-space.js",
        "feature-weather-alert.js",
        "login.js",
        "spectrum.js",
    }
)
_CONTENT_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".webmanifest": "application/manifest+json",
}


class AssetManifestError(RuntimeError):
    """Raised when packaged build metadata or an asset fails validation."""


@dataclass(frozen=True)
class BuiltAsset:
    """Immutable metadata for one content-addressed browser asset."""

    path: str
    bytes: int
    sha256: str
    integrity: str


@dataclass(frozen=True)
class AssetManifest:
    """Validated dashboard build manifest."""

    schema: int
    assets: Mapping[str, BuiltAsset]


def _normalise_resource_path(path: str) -> str:
    if not isinstance(path, str) or not path or "\\" in path:
        raise ValueError("dashboard resource path must be a nonempty POSIX path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute():
        raise ValueError("dashboard resource path must be relative")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("dashboard resource path may not contain traversal segments")
    return parsed.as_posix()


@lru_cache(maxsize=128)
def _read_packaged_resource(path: str) -> bytes:
    resource = resources.files(_PACKAGE).joinpath("static")
    for part in path.split("/"):
        resource = resource.joinpath(part)
    if not resource.is_file():
        raise FileNotFoundError(path)
    return resource.read_bytes()


def read_static_bytes(path: str) -> bytes:
    """Read one packaged static resource after rejecting path traversal."""
    return _read_packaged_resource(_normalise_resource_path(path))


def read_static_text(path: str) -> str:
    """Read one UTF-8 packaged static resource."""
    return read_static_bytes(path).decode("utf-8")


def static_content_type(path: str) -> str:
    """Return the explicit browser media type for a packaged resource."""
    suffix = PurePosixPath(path).suffix.lower()
    return _CONTENT_TYPES.get(suffix, "application/octet-stream")


def _require_string(entry: Mapping[str, Any], field: str, logical_name: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise AssetManifestError(f"{logical_name}: {field} must be a nonempty string")
    return value


def _parse_manifest(data: Any) -> AssetManifest:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise AssetManifestError("dashboard asset manifest must use schema 1")
    raw_assets = data.get("assets")
    if not isinstance(raw_assets, dict):
        raise AssetManifestError("dashboard asset manifest requires an assets object")
    missing = _REQUIRED_ASSETS - raw_assets.keys()
    if missing:
        raise AssetManifestError(
            f"dashboard asset manifest is missing: {', '.join(sorted(missing))}"
        )

    parsed: dict[str, BuiltAsset] = {}
    for logical_name, raw_entry in raw_assets.items():
        if not isinstance(logical_name, str) or not isinstance(raw_entry, dict):
            raise AssetManifestError("dashboard asset manifest contains an invalid entry")
        path = _require_string(raw_entry, "path", logical_name)
        try:
            path = _normalise_resource_path(path)
        except ValueError as exc:
            raise AssetManifestError(f"{logical_name}: invalid asset path") from exc
        if not path.startswith("assets/"):
            raise AssetManifestError(f"{logical_name}: built assets must live below assets/")
        if PurePosixPath(logical_name).suffix != PurePosixPath(path).suffix:
            raise AssetManifestError(f"{logical_name}: asset extension does not match logical name")

        byte_count = raw_entry.get("bytes")
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise AssetManifestError(f"{logical_name}: bytes must be a nonnegative integer")
        sha256 = _require_string(raw_entry, "sha256", logical_name)
        if not _SHA256.fullmatch(sha256):
            raise AssetManifestError(f"{logical_name}: sha256 must be lowercase hexadecimal")
        integrity = _require_string(raw_entry, "integrity", logical_name)

        try:
            content = read_static_bytes(path)
        except FileNotFoundError as exc:
            raise AssetManifestError(f"{logical_name}: packaged asset is missing: {path}") from exc
        actual_sha = hashlib.sha256(content).hexdigest()
        expected_integrity = "sha256-" + base64.b64encode(bytes.fromhex(actual_sha)).decode("ascii")
        if len(content) != byte_count or actual_sha != sha256 or integrity != expected_integrity:
            raise AssetManifestError(f"{logical_name}: packaged asset does not match its manifest")
        parsed[logical_name] = BuiltAsset(path, byte_count, sha256, integrity)

    return AssetManifest(schema=1, assets=MappingProxyType(parsed))


@lru_cache(maxsize=1)
def load_asset_manifest() -> AssetManifest:
    """Load and fully verify the packaged dashboard build manifest once."""
    try:
        data = json.loads(read_static_text("asset-manifest.json"))
    except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AssetManifestError("dashboard asset manifest is missing or malformed") from exc
    return _parse_manifest(data)


def asset_url(logical_name: str) -> str:
    """Resolve a logical build asset name to its content-addressed URL."""
    try:
        record = load_asset_manifest().assets[logical_name]
    except KeyError as exc:
        raise AssetManifestError(f"unknown dashboard asset: {logical_name}") from exc
    return f"/static/{record.path}"


def built_asset_urls() -> tuple[str, ...]:
    """Return every content-addressed asset URL in stable logical-name order."""
    manifest = load_asset_manifest()
    return tuple(asset_url(name) for name in sorted(manifest.assets))


def shell_asset_urls() -> tuple[str, ...]:
    """Return eager shell assets, excluding panel chunks loaded on first use."""
    manifest = load_asset_manifest()
    return tuple(
        asset_url(name) for name in sorted(manifest.assets) if not name.startswith("feature-")
    )


def render_template(path: str, *, ready_features: frozenset[str] = frozenset()) -> str:
    """Render asset placeholders and first-paint feature availability."""

    unknown = ready_features - _KNOWN_FEATURES
    if unknown:
        raise AssetManifestError(f"unknown dashboard feature state: {', '.join(sorted(unknown))}")
    source = read_static_text(path)

    def replace(match: re.Match[str]) -> str:
        return asset_url(match.group(1))

    rendered = _ASSET_PLACEHOLDER.sub(replace, source)
    rendered = _FEATURE_PLACEHOLDER.sub(
        lambda match: "" if match.group(1) in ready_features else "csp-initial-hidden",
        rendered,
    )
    rendered = rendered.replace(
        _READY_FEATURES_PLACEHOLDER,
        " ".join(sorted(ready_features)),
    )
    if "{{ASSET:" in rendered:
        raise AssetManifestError(f"{path}: malformed dashboard asset placeholder")
    if "{{FEATURE:" in rendered or _READY_FEATURES_PLACEHOLDER in rendered:
        raise AssetManifestError(f"{path}: malformed dashboard feature placeholder")
    return rendered
