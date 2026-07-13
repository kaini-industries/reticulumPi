#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Dynamic NomadNet page: scoped local network summary."""

import json
import os
import time
import urllib.error
import urllib.request


DASHBOARD = os.environ.get("RETICULUMPI_DASHBOARD_URL", "http://127.0.0.1:8080")
TOKEN_FILE = os.environ.get(
    "RETICULUMPI_LOCAL_API_TOKEN_FILE",
    "/run/reticulumpi/local_api.token",
)
FETCH_TIMEOUT = 5


def fetch_status():
    """Fetch the fixed-scope status resource using the local-service token."""
    with open(TOKEN_FILE, encoding="utf-8") as token_file:
        token = token_file.read(512).strip()
    request = urllib.request.Request(
        DASHBOARD + "/api/status",
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "ReticulumPi-NomadNet/1.1",
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


print("`!`F222`Bddd`cNetwork Status`!")
print("`c" + time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime()))
print("-")
print("`a`b`f")

try:
    payload = fetch_status()
    if not payload.get("ok"):
        raise ValueError(payload.get("error", "API returned an error"))
    status = payload.get("data", {})
    plugins = status.get("plugins", {})
    network = plugins.get("network_map", {})
    failed = status.get("failed_plugins", [])

    print(">Node")
    print("")
    print("  Version      : " + str(status.get("version", "?")))
    print("  Known nodes  : " + str(network.get("known_nodes", "unavailable")))
    print("  Plugins      : " + str(len(plugins)))
    print("  Failed       : " + str(len(failed)))
    print("")
    print(">Network Services")
    print("")
    for name in ("transport_monitor", "hub_discovery", "path_warmer", "network_map"):
        entry = plugins.get(name)
        if entry is None:
            state = "not enabled"
        else:
            lifecycle = entry.get("_lifecycle", {}) if isinstance(entry, dict) else {}
            state = lifecycle.get("state") or ("active" if entry.get("active") else "loaded")
        print(f"  {name:<20} {state}")
    if failed:
        print("")
        print(">Failed Plugins")
        print("")
        for item in failed[:10]:
            print("  " + str(item.get("name", "unknown")))

except FileNotFoundError:
    print(">Local API token unavailable")
    print("")
    print("  Enable web_dashboard.local_api and ensure")
    print("  NomadNet can read the configured token file.")
except urllib.error.HTTPError as exc:
    print(">Dashboard authentication failed")
    print("")
    print(f"  HTTP {exc.code}; verify local_api scope and token permissions.")
except (urllib.error.URLError, TimeoutError):
    print(">Dashboard unavailable")
    print("")
    print("  The local dashboard did not respond within five seconds.")
except (ValueError, KeyError, json.JSONDecodeError) as exc:
    print(">Status response invalid")
    print("")
    print("  " + str(exc)[:120])

print("")
print("-")
print("`cReticulumPi scoped local status")
