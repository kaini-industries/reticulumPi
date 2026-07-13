#!/usr/bin/env python3
"""Verify that a built wheel is installable and contains runtime assets."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import subprocess
import tempfile
import venv
import zipfile
from pathlib import Path


REQUIRED_ASSETS = {
    "reticulumpi/data/meshchat_launcher.pydata",
    "reticulumpi/builtin_plugins/web_dashboard/static/asset-manifest.json",
    "reticulumpi/builtin_plugins/web_dashboard/static/index.html",
    "reticulumpi/builtin_plugins/web_dashboard/static/login.html",
    "reticulumpi/builtin_plugins/web_dashboard/static/manifest.webmanifest",
    "reticulumpi/builtin_plugins/web_dashboard/static/spectrum.html",
    "reticulumpi/builtin_plugins/web_dashboard/static/style.css",
    "reticulumpi/builtin_plugins/web_dashboard/static/sw.js",
    "reticulumpi/builtin_plugins/web_dashboard/static/vendor/leaflet.js",
    "reticulumpi/builtin_plugins/web_dashboard/static/vendor/images/marker-icon.png",
}
REQUIRED_DATA_SUFFIXES = {
    "share/reticulumpi/nomadnet/pages/help.mu",
    "share/reticulumpi/nomadnet/pages/index.mu",
    "share/reticulumpi/nomadnet/pages/network.mu",
    "share/reticulumpi/nomadnet/pages/status.mu",
}
STATIC_PREFIX = "reticulumpi/builtin_plugins/web_dashboard/static/"
REQUIRED_BUILT_ASSETS = {
    "dashboard.css",
    "dashboard.js",
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
    "login.js",
    "spectrum.js",
}

INSTALLED_DASHBOARD_SMOKE = r"""
import asyncio
import hashlib
import json
import socket
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from aiohttp import web

import reticulumpi
from importlib.metadata import version
from reticulumpi._paths import find_distribution_asset
from reticulumpi.builtin_plugins.web_dashboard.auth import AuthManager
from reticulumpi.builtin_plugins.web_dashboard.server import create_app
from reticulumpi.plugin_loader import PluginLoader


PASSWORD = "wheel-smoke-password"

assert version("reticulumpi") == reticulumpi.__version__

builtin_directory = files("reticulumpi").joinpath("builtin_plugins")
discovered_plugins = PluginLoader().discover([str(builtin_directory)])
required_plugins = {
    "file_transfer",
    "messaging_hub",
    "nomadnet_server",
    "web_dashboard",
}
missing_plugins = required_plugins - discovered_plugins.keys()
assert not missing_plugins, f"installed wheel plugin discovery missed: {sorted(missing_plugins)}"

launcher = files("reticulumpi").joinpath("data/meshchat_launcher.pydata")
assert launcher.is_file(), launcher
for page in ("help.mu", "index.mu", "network.mu", "status.mu"):
    installed_page = find_distribution_asset("nomadnet", "pages", page)
    assert installed_page is not None, page
    assert Path(installed_page).is_file(), installed_page


async def smoke():
    core = SimpleNamespace(
        _get_version=lambda: reticulumpi.__version__,
        get_status=lambda: {
            "version": reticulumpi.__version__,
            "plugins": {},
            "failed_plugins": [],
            "wheel_smoke": True,
        },
        plugins={},
    )
    plugin = SimpleNamespace(
        app=core,
        config={
            "local_api": {"enabled": False},
            "ssl": {},
            "tile_proxy": {"enabled": False},
            "tile_cache_entries": 5000,
            "ws_compress": False,
        },
        _auth=AuthManager(
            plaintext_password=PASSWORD,
            session_timeout=60,
            max_sessions=2,
        ),
        _local_api_token=None,
    )
    app = create_app(plugin)
    # This smoke targets installed routes/resources. Avoid starting the full
    # metrics collectors, which require a live Reticulum application.
    app.on_startup.clear()
    app.on_shutdown.clear()

    runner = web.AppRunner(app, handle_signals=False)
    await runner.setup()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.setblocking(False)
    port = sock.getsockname()[1]
    site = web.SockSite(runner, sock)
    await site.start()

    try:
        timeout = aiohttp.ClientTimeout(total=5)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=jar) as client:
            base = f"http://127.0.0.1:{port}"

            async with client.get(base + "/login.html") as response:
                assert response.status == 200
                assert response.content_type == "text/html"
                login_html = await response.text()
                assert "/static/assets/" in login_html
                assert "/static/manifest.webmanifest" in login_html

            async with client.post(
                base + "/api/auth/login",
                json={"password": PASSWORD},
            ) as response:
                assert response.status == 200
                payload = await response.json()
                assert payload["ok"] is True
                assert "session" in response.cookies

            async with client.get(base + "/") as response:
                assert response.status == 200
                dashboard_html = await response.text()
                assert "{{ASSET:" not in dashboard_html
                assert "/static/assets/" in dashboard_html
                assert "/static/vendor/leaflet.js" not in dashboard_html
                assert "/static/vendor/leaflet.css" not in dashboard_html

            async with client.get(base + "/static/asset-manifest.json") as response:
                assert response.status == 200
                assert response.content_type == "application/json"
                build_manifest = await response.json()
                assert build_manifest["schema"] == 1

            async with client.get(base + "/static/manifest.webmanifest") as response:
                assert response.status == 200
                assert response.content_type == "application/manifest+json"
                pwa_manifest = await response.json()
                assert pwa_manifest["start_url"] == "/"

            async with client.get(base + "/static/version.js") as response:
                assert response.status == 200
                assert response.content_type == "application/javascript"
                assert response.headers["Cache-Control"] == "no-cache"
                assert json.dumps(reticulumpi.__version__) in await response.text()

            dashboard_asset = build_manifest["assets"]["dashboard.js"]
            async with client.get(base + "/static/" + dashboard_asset["path"]) as response:
                assert response.status == 200
                assert response.content_type == "application/javascript"
                assert "immutable" in response.headers.get("Cache-Control", "")
                content = await response.read()
                assert len(content) == dashboard_asset["bytes"]
                assert hashlib.sha256(content).hexdigest() == dashboard_asset["sha256"]

            for logical_name in sorted(
                name for name in build_manifest["assets"] if name.startswith("feature-")
            ):
                feature_asset = build_manifest["assets"][logical_name]
                assert "/static/" + feature_asset["path"] not in dashboard_html
                async with client.get(base + "/static/" + feature_asset["path"]) as response:
                    assert response.status == 200
                    assert response.content_type == "application/javascript"
                    content = await response.read()
                    assert len(content) == feature_asset["bytes"]
                    assert hashlib.sha256(content).hexdigest() == feature_asset["sha256"]

            async with client.get(base + "/api/status") as response:
                assert response.status == 200
                assert response.headers["X-Content-Type-Options"] == "nosniff"
                status = await response.json()
                assert status["ok"] is True
                assert status["data"]["wheel_smoke"] is True
    finally:
        await runner.cleanup()


async def bounded_smoke():
    await asyncio.wait_for(smoke(), timeout=20)


asyncio.run(bounded_smoke())
print("Installed dashboard HTTP smoke passed")
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify an installed ReticulumPi wheel and its packaged assets.",
    )
    parser.add_argument("wheel", type=Path)
    parser.add_argument(
        "--requirements",
        type=Path,
        help="hashed dependency profile to install before the wheel",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    wheel = args.wheel.resolve()
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = REQUIRED_ASSETS - names
        if missing:
            raise SystemExit(f"wheel is missing runtime assets: {', '.join(sorted(missing))}")
        for suffix in sorted(REQUIRED_DATA_SUFFIXES):
            if not any(name.endswith(suffix) for name in names):
                raise SystemExit(f"wheel is missing runtime data: {suffix}")

        manifest = json.loads(archive.read(STATIC_PREFIX + "asset-manifest.json"))
        if manifest.get("schema") != 1 or not isinstance(manifest.get("assets"), dict):
            raise SystemExit("wheel contains an invalid dashboard asset manifest")
        missing_built = REQUIRED_BUILT_ASSETS - manifest["assets"].keys()
        if missing_built:
            raise SystemExit(
                "wheel manifest is missing built assets: " + ", ".join(sorted(missing_built))
            )
        manifest_paths = {
            STATIC_PREFIX + metadata["path"] for metadata in manifest["assets"].values()
        }
        packaged_built = {name for name in names if name.startswith(STATIC_PREFIX + "assets/")}
        orphaned = packaged_built - manifest_paths
        if orphaned:
            raise SystemExit(
                "wheel contains stale unmanifested built assets: " + ", ".join(sorted(orphaned))
            )
        for logical_name, metadata in manifest["assets"].items():
            path = STATIC_PREFIX + metadata["path"]
            if path not in names:
                raise SystemExit(f"wheel is missing {logical_name}: {path}")
            content = archive.read(path)
            digest = hashlib.sha256(content).digest()
            integrity = "sha256-" + base64.b64encode(digest).decode("ascii")
            if (
                len(content) != metadata["bytes"]
                or digest.hex() != metadata["sha256"]
                or integrity != metadata["integrity"]
            ):
                raise SystemExit(f"wheel asset failed manifest verification: {logical_name}")

    with tempfile.TemporaryDirectory(prefix="reticulumpi-wheel-") as raw:
        environment = Path(raw) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / "bin/python"
        pip = environment / "bin/pip"
        if args.requirements is not None:
            requirements = args.requirements.resolve()
            subprocess.run(
                [
                    pip,
                    "install",
                    "--require-hashes",
                    "--only-binary",
                    ":all:",
                    "--requirement",
                    requirements,
                ],
                check=True,
                timeout=180,
            )
            subprocess.run(
                [pip, "install", "--no-deps", wheel],
                check=True,
                timeout=60,
            )
        else:
            subprocess.run(
                [pip, "install", f"{wheel}[dashboard]"],
                check=True,
                timeout=180,
            )
        subprocess.run([pip, "check"], check=True, timeout=30)
        subprocess.run(
            [
                python,
                "-I",
                "-c",
                INSTALLED_DASHBOARD_SMOKE,
            ],
            cwd=raw,
            check=True,
            timeout=30,
        )
    print(f"Verified installed wheel, dashboard HTTP routes, and assets: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
