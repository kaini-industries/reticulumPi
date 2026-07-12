"""Dashboard build-manifest, package-resource, and cache-policy contracts."""

from __future__ import annotations

import gzip
import json
import re
import tomllib
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import aiohttp.web
import pytest
from aiohttp.test_utils import make_mocked_request

from reticulumpi.builtin_plugins.web_dashboard import assets as assets_module
from reticulumpi.builtin_plugins.web_dashboard.assets import (
    AssetManifestError,
    _normalise_resource_path,
    _parse_manifest,
    asset_url,
    built_asset_urls,
    load_asset_manifest,
    read_static_bytes,
    read_static_text,
    render_template,
    shell_asset_urls,
    static_content_type,
)
from reticulumpi.builtin_plugins.web_dashboard.keys import PLUGIN_KEY
from reticulumpi.builtin_plugins.web_dashboard.server import (
    _apply_security_headers,
    _ready_dashboard_features,
    _serve_static,
    _serve_sw,
)


ROOT = Path(__file__).parents[1]
STATIC_ROOT = ROOT / "src" / "reticulumpi" / "builtin_plugins" / "web_dashboard" / "static"
FEATURE_ASSETS = {
    "feature-acars.js",
    "feature-adsb.js",
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
}
REQUIRED_ASSETS = {
    "dashboard.css",
    "dashboard.js",
    "login.js",
    "spectrum.js",
    *FEATURE_ASSETS,
}


def test_first_party_dashboard_sources_do_not_require_inline_csp_allowances():
    """Keep style-src/script-src strict as dashboard panels evolve."""
    for template in ("index.html", "login.html", "spectrum.html"):
        html = (STATIC_ROOT / template).read_text()
        assert re.search(r"<style\b", html, re.IGNORECASE) is None
        assert re.search(r"\sstyle\s*=", html, re.IGNORECASE) is None
        assert (
            re.search(
                r"\son(?:click|change|input|submit|load|error|focus|blur|keydown|keyup|pointerdown|mousedown|touchstart)\s*=",
                html,
                re.IGNORECASE,
            )
            is None
        )

    for source_path in STATIC_ROOT.glob("*.js"):
        source = source_path.read_text()
        assert re.search(r"<style\b", source, re.IGNORECASE) is None, source_path.name
        assert "style=" not in source, source_path.name
        assert ".style.cssText" not in source, source_path.name
        assert re.search(r"\.style\s*=", source) is None, source_path.name
        assert re.search(r"setAttribute\s*\(\s*['\"]style['\"]", source) is None, source_path.name
        assert (
            re.search(
                r"<[^<>]{0,512}\son(?:click|change|input|submit|load|error|focus|blur|keydown|keyup|pointerdown|mousedown|touchstart)=",
                source,
                re.IGNORECASE,
            )
            is None
        ), source_path.name

    manifest = load_asset_manifest()
    for logical_name, asset in manifest.assets.items():
        if not asset.path.endswith(".js"):
            continue
        source = read_static_text(asset.path)
        assert re.search(r"<style\b", source, re.IGNORECASE) is None, logical_name
        assert re.search(r"<[^<>]{0,512}\sstyle=\\?['\"]", source, re.IGNORECASE) is None, (
            logical_name
        )
        assert ".style.cssText" not in source, logical_name
        assert re.search(r"\.style\s*=", source) is None, logical_name
        assert (
            re.search(
                r"<[^<>]{0,512}\son(?:click|change|input|submit)=\\?['\"]",
                source,
                re.IGNORECASE,
            )
            is None
        ), logical_name


def test_manifest_verifies_every_content_addressed_asset():
    manifest = load_asset_manifest()

    assert manifest.schema == 1
    assert REQUIRED_ASSETS <= manifest.assets.keys()
    for logical_name, asset in manifest.assets.items():
        assert re.fullmatch(r"assets/[a-z-]+-[A-Z0-9]+\.(?:css|js)", asset.path)
        assert asset.path.endswith(Path(logical_name).suffix)
        assert len(read_static_bytes(asset.path)) == asset.bytes
        assert re.fullmatch(r"[0-9a-f]{64}", asset.sha256)
        assert asset.integrity.startswith("sha256-")


def test_manifest_rejects_resource_traversal():
    manifest = load_asset_manifest()
    data = {
        "schema": manifest.schema,
        "assets": {name: asdict(asset) for name, asset in manifest.assets.items()},
    }
    data["assets"]["dashboard.js"]["path"] = "../dashboard.js"

    with pytest.raises(AssetManifestError, match="invalid asset path"):
        _parse_manifest(data)


@pytest.mark.parametrize("path", ["", "assets\\app.js", "/assets/app.js", "assets//app.js"])
def test_resource_path_normalization_rejects_non_posix_and_absolute_paths(path):
    with pytest.raises(ValueError, match="dashboard resource path"):
        _normalise_resource_path(path)


def _manifest_data():
    manifest = load_asset_manifest()
    return {
        "schema": manifest.schema,
        "assets": {name: asdict(asset) for name, asset in manifest.assets.items()},
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(schema=2), "schema 1"),
        (lambda data: data.update(assets=[]), "assets object"),
        (lambda data: data["assets"].pop("dashboard.js"), "is missing"),
        (lambda data: data["assets"].update({1: {}}), "invalid entry"),
        (
            lambda data: data["assets"]["dashboard.js"].update(path="dashboard.js"),
            "below assets",
        ),
        (
            lambda data: data["assets"]["dashboard.css"].update(
                path=data["assets"]["dashboard.js"]["path"]
            ),
            "extension does not match",
        ),
        (lambda data: data["assets"]["dashboard.js"].update(bytes=True), "nonnegative"),
        (lambda data: data["assets"]["dashboard.js"].update(sha256="XYZ"), "lowercase"),
        (lambda data: data["assets"]["dashboard.js"].update(integrity="bad"), "does not match"),
        (lambda data: data["assets"]["dashboard.js"].update(path=""), "nonempty string"),
    ],
)
def test_manifest_rejects_malformed_metadata(mutate, message):
    data = deepcopy(_manifest_data())
    mutate(data)
    with pytest.raises(AssetManifestError, match=message):
        _parse_manifest(data)


def test_manifest_rejects_missing_packaged_asset():
    data = deepcopy(_manifest_data())
    data["assets"]["dashboard.js"].update(path="assets/missing-ABC.js")
    with pytest.raises(AssetManifestError, match="packaged asset is missing"):
        _parse_manifest(data)


def test_manifest_loader_and_asset_lookup_fail_closed(monkeypatch):
    load_asset_manifest.cache_clear()
    monkeypatch.setattr(assets_module, "read_static_text", lambda _path: "{")
    with pytest.raises(AssetManifestError, match="missing or malformed"):
        load_asset_manifest()
    load_asset_manifest.cache_clear()

    monkeypatch.undo()
    with pytest.raises(AssetManifestError, match="unknown dashboard asset"):
        asset_url("unknown.js")


def test_template_rejects_unparsed_asset_placeholder(monkeypatch):
    monkeypatch.setattr(assets_module, "read_static_text", lambda _path: "{{ASSET:broken")
    with pytest.raises(AssetManifestError, match="malformed dashboard asset placeholder"):
        render_template("broken.html")


def test_dashboard_template_hides_unready_features_before_first_paint():
    html = render_template("index.html", ready_features=frozenset({"gps", "messages"}))

    assert 'data-ready-features="gps messages"' in html
    assert re.search(r'class="[^"]*csp-initial-hidden[^"]*" id="hotspot-section"', html)
    assert not re.search(r'class="[^"]*csp-initial-hidden[^"]*" id="gps-section"', html)
    assert not re.search(r'class="[^"]*csp-initial-hidden[^"]*" id="msg-lxmf-section"', html)
    assert "{{FEATURE:" not in html
    assert "{{READY_FEATURES}}" not in html


def test_dashboard_template_rejects_unknown_ready_feature():
    with pytest.raises(AssetManifestError, match="unknown dashboard feature state"):
        render_template("index.html", ready_features=frozenset({"future-feature"}))


def test_first_paint_features_are_derived_only_from_ready_plugins():
    class Core:
        plugins = {}

        def get_ready_plugin(self, name):
            if name in {"gps_telemetry", "meshcore_observer", "messaging_hub"}:
                return object()
            return None

    features = _ready_dashboard_features(SimpleNamespace(app=Core()))

    assert {"gps", "map", "meshcore", "messages"} <= features
    assert "radio" not in features


def test_packaged_resource_reader_rejects_traversal():
    with pytest.raises(ValueError, match="traversal"):
        read_static_bytes("vendor/../index.html")


def test_pwa_manifest_is_packaged_linked_and_has_explicit_media_type():
    manifest = json.loads(read_static_text("manifest.webmanifest"))

    assert manifest["id"] == "/"
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert static_content_type("manifest.webmanifest") == "application/manifest+json"
    for template in ("index.html", "login.html", "spectrum.html"):
        assert '<link rel="manifest" href="/static/manifest.webmanifest">' in render_template(
            template
        )


@pytest.mark.asyncio
async def test_static_handler_serves_pwa_manifest_with_manifest_media_type():
    request = make_mocked_request(
        "GET",
        "/static/manifest.webmanifest",
        match_info={"asset_path": "manifest.webmanifest"},
    )

    response = await _serve_static(request)

    assert response.content_type == "application/manifest+json"
    assert json.loads(response.body)["name"] == "ReticulumPi Dashboard"


@pytest.mark.parametrize(
    ("template", "logical_name"),
    [
        ("index.html", "dashboard.js"),
        ("login.html", "login.js"),
        ("spectrum.html", "spectrum.js"),
    ],
)
def test_templates_resolve_only_their_manifest_bundle(template, logical_name):
    html = render_template(template)

    assert "{{ASSET:" not in html
    assert asset_url("dashboard.css") in html
    assert asset_url(logical_name) in html
    assert "/static/app.js" not in html
    assert "/static/style.css" not in html


def test_dashboard_critical_shell_stays_within_request_and_transfer_budgets():
    html = render_template("index.html")
    static_references = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    # The coordinator fetches the build manifest once before the first
    # optional import; count that request as part of the critical ceiling.
    assert len(static_references) + 1 <= 6

    paths = [reference.removeprefix("/static/") for reference in static_references]
    compressed_bytes = len(gzip.compress(html.encode("utf-8")))
    compressed_bytes += sum(len(gzip.compress(read_static_bytes(path))) for path in paths)
    compressed_bytes += len(gzip.compress(read_static_bytes("asset-manifest.json")))
    assert compressed_bytes <= 180 * 1024


@pytest.mark.asyncio
async def test_service_worker_precaches_shell_but_not_lazy_features_or_private_data():
    app = aiohttp.web.Application()
    app[PLUGIN_KEY] = SimpleNamespace(config={"tile_cache_entries": 250})
    request = make_mocked_request("GET", "/sw.js", app=app)

    response = await _serve_sw(request)
    source = response.text

    for url in shell_asset_urls():
        assert url in source
    for logical_name in FEATURE_ASSETS:
        assert asset_url(logical_name) not in source
    assert "/static/vendor/leaflet.css" not in source
    assert "/static/vendor/leaflet.js" not in source
    assert "/static/vendor/images/marker-icon.png" not in source
    assert "/*__RPI_BUILT_ASSETS__*/" not in source
    assert "'/static/app.js'" not in source
    assert "'/static/asset-manifest.json'" in source
    assert "'/static/manifest.webmanifest'" in source
    assert "if (_isPrivateEndpoint(path)) return;" in source
    assert "path.indexOf('/api/') === 0" in source
    assert "path.indexOf('/auth/') === 0" in source
    assert response.headers["Cache-Control"] == "no-cache"


def test_optional_chunks_are_absent_from_initial_html_and_core_bundle():
    html = render_template("index.html")
    core = read_static_text(load_asset_manifest().assets["dashboard.js"].path)

    for logical_name in FEATURE_ASSETS:
        assert asset_url(logical_name) not in html
    assert "/static/vendor/leaflet.css" not in html
    assert "/static/vendor/leaflet.js" not in html
    assert "/static/asset-manifest.json" in core
    assert "import(" in core
    # Leaflet remains outside the shell and is injected only by the feature
    # dependency loader immediately before a map-backed chunk is imported.
    assert "/static/vendor/leaflet.css" in core
    assert "/static/vendor/leaflet.js" in core
    # Sentinels from the three largest extracted feature families prove they
    # were not accidentally folded back into the coordinator bundle.
    assert "/api/messages/conversations" not in core
    assert "adsb-photo-" not in core
    assert "radio_record_start" not in core


def test_feature_entries_export_explicit_lifecycle_contracts():
    for name in (
        "acars",
        "adsb",
        "ais",
        "gps",
        "hotspot",
        "link-tester",
        "lora",
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
    ):
        source = (ROOT / "tools" / "dashboard" / f"feature-{name}-entry.js").read_text()
        assert "export function init(context)" in source
        assert "export function dispose" in source


def test_legacy_panels_are_not_imported_by_core_entry():
    source = (ROOT / "tools" / "dashboard" / "dashboard-entry.js").read_text()

    assert 'static/app.js"' in source
    for module in (
        "acars",
        "ais",
        "gps",
        "hotspot",
        "link_tester",
        "lora",
        "mesh",
        "mesh_bridge_panel",
        "meshcore",
        "meshtastic",
        "noaa",
        "ntp",
        "radiosonde",
        "routing",
        "weather_alert",
    ):
        assert f'static/{module}.js"' not in source


def test_all_built_urls_partition_into_shell_and_lazy_features():
    feature_urls = {asset_url(name) for name in FEATURE_ASSETS}

    assert set(built_asset_urls()) == set(shell_asset_urls()) | feature_urls
    assert set(shell_asset_urls()).isdisjoint(feature_urls)


def test_optional_feature_apis_are_absent_from_boot_fallbacks():
    source = (
        ROOT / "src" / "reticulumpi" / "builtin_plugins" / "web_dashboard" / "static" / "app.js"
    ).read_text()
    critical = source.split("function fetchCritical()", 1)[1].split("function fetchSecondary()", 1)[
        0
    ]
    secondary = source.split("function fetchSecondary()", 1)[1].split(
        "function fetchWsUncovered()", 1
    )[0]

    optional_paths = (
        "/api/adsb",
        "/api/gps",
        "/api/mesh/telemetry",
        "/api/meshcore/",
        "/api/meshtastic/",
    )
    assert all(path not in critical for path in optional_paths)
    assert all(path not in secondary for path in optional_paths)
    assert all(path in source for path in optional_paths)
    assert "function _fetchFeatureData(name)" in source


@pytest.mark.parametrize(
    "path",
    [
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/password",
        "/api/config",
        "/api/services/restart",
        "/api/services/restart/operation-id",
    ],
)
def test_sensitive_api_responses_are_never_cacheable(path):
    request = make_mocked_request("POST", path)
    response = aiohttp.web.Response()

    _apply_security_headers(request, response)

    assert response.headers["Cache-Control"] == "private, no-store"


def test_content_addressed_assets_are_immutable_public_resources():
    request = make_mocked_request("GET", asset_url("dashboard.js"))
    response = aiohttp.web.Response()

    _apply_security_headers(request, response)

    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_unsuccessful_api_responses_are_not_cacheable():
    request = make_mocked_request("GET", "/api/status")
    response = aiohttp.web.Response(status=428)

    _apply_security_headers(request, response)

    assert response.headers["Cache-Control"] == "private, no-store"


def test_wheel_package_data_includes_generated_asset_directory():
    with (ROOT / "pyproject.toml").open("rb") as handle:
        package_data = tomllib.load(handle)["tool"]["setuptools"]["package-data"]["reticulumpi"]

    assert "builtin_plugins/web_dashboard/static/assets/*" in package_data


def test_browser_fixture_accepts_pep440_local_service_worker_versions():
    from tools.dashboard_browser_server import _SAFE_VERSION_RE

    assert _SAFE_VERSION_RE.fullmatch("v0.post1.dev215+unknown.g9e100ad40.d20260711")
    assert _SAFE_VERSION_RE.fullmatch("interrupted-update")
    assert _SAFE_VERSION_RE.fullmatch("bad'version") is None
    source = (ROOT / "tools" / "dashboard_browser_server.py").read_text(encoding="utf-8")
    assert "max_sessions=128" in source
