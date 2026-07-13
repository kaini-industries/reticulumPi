"""Regression contracts for availability- and visibility-gated dashboard chunks."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
STATIC = ROOT / "src/reticulumpi/builtin_plugins/web_dashboard/static"
TOOLS = ROOT / "tools/dashboard"

LAZY_FEATURES = {
    "acars": "acars.js",
    "adsb": "adsb.js",
    "ais": "ais.js",
    "gps": "gps.js",
    "hotspot": "hotspot.js",
    "link-tester": "link_tester.js",
    "lora": "lora.js",
    "map": "map.js",
    "mesh": "mesh.js",
    "mesh-bridge": "mesh_bridge_panel.js",
    "meshcore": "meshcore.js",
    "messages": "messages_panel.js",
    "meshtastic": "meshtastic.js",
    "noaa": "noaa.js",
    "ntp": "ntp.js",
    "radio": "radio.js",
    "radiosonde": "radiosonde.js",
    "routing": "routing.js",
    "space": "space.js",
    "weather-alert": "weather_alert.js",
}


def test_core_entry_contains_only_coordinator_and_error_boundary():
    source = (TOOLS / "dashboard-entry.js").read_text()
    imports = [line.strip() for line in source.splitlines() if line.strip().startswith("import ")]

    assert imports == [
        'import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/json_fetch.js";',
        'import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/errlog.js";',
        'import "../../src/reticulumpi/builtin_plugins/web_dashboard/static/app.js";',
    ]


def test_every_optional_panel_has_a_manifested_lifecycle_chunk():
    manifest = json.loads((STATIC / "asset-manifest.json").read_text())["assets"]
    coordinator = (STATIC / "app.js").read_text()

    for feature, module in LAZY_FEATURES.items():
        logical_name = f"feature-{feature}.js"
        entry = (TOOLS / f"feature-{feature}-entry.js").read_text()
        assert logical_name in manifest
        assert f'static/{module}"' in entry
        assert "export function init(context)" in entry
        assert "export function dispose" in entry
        assert f"asset: '{logical_name}'" in coordinator


def test_optional_chunks_require_availability_and_open_or_visible_intent():
    source = (STATIC / "app.js").read_text()

    assert "if (!feature.available)" in source
    assert "Feature is unavailable:" in source
    assert "_featureHasOpenBody(feature)" in source
    assert "feature.loadWhenVisible" in source
    assert "IntersectionObserver" in source
    assert "_featureIsVisible(feature)" in source
    assert "event.stopImmediatePropagation()" in source
    assert "loadFeature(name).then(function()" in source
    assert "document.body.dataset.readyFeatures" in source
    assert "available: !!_serverReadyFeatures[name]" in source


def test_scrollable_regions_are_keyboard_focusable_and_named():
    source = (STATIC / "app.js").read_text()
    styles = (STATIC / "style.css").read_text()

    assert "var scrollableRegions = document.querySelectorAll" in source
    assert "scrollRegion.tabIndex = 0" in source
    assert "scrollRegion.setAttribute('role', 'region')" in source
    assert "scrollRegion.setAttribute('aria-label'" in source
    assert ".section { overflow-x: auto" not in styles


def test_initial_fetch_tiers_do_not_request_optional_panel_snapshots():
    source = (STATIC / "app.js").read_text()
    critical = source.split("function fetchCritical()", 1)[1].split("function fetchSecondary()", 1)[
        0
    ]
    secondary = source.split("function fetchSecondary()", 1)[1].split(
        "function fetchWsUncovered()", 1
    )[0]
    eager = critical + secondary

    for endpoint in (
        "/api/acars",
        "/api/adsb",
        "/api/ais",
        "/api/captive_portal",
        "/api/gps",
        "/api/link_tester",
        "/api/mesh/",
        "/api/mesh_bridge/",
        "/api/meshcore/",
        "/api/meshtastic/",
        "/api/noaa",
        "/api/ntp",
        "/api/radiosonde",
        "/api/routing",
        "/api/weather_alert",
    ):
        assert endpoint not in eager
        assert endpoint in source


def test_spectrum_navigation_fails_closed_until_a_backing_plugin_is_ready():
    html = (STATIC / "index.html").read_text()
    source = (STATIC / "app.js").read_text()

    assert 'id="spectrum-nav-link" hidden' in html
    assert "spectrumLink.hidden = !spectrumAvailable" in source
    for plugin in ("spectrum_scanner", "lora_scanner", "lora_link_tester"):
        assert f"_pluginIsReady(plugins, '{plugin}')" in source
