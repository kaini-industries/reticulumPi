#!/opt/reticulumpi/.venv/bin/python3
# -*- coding: utf-8 -*-
"""Dynamic NomadNet page: Network Reachability

Shows known mesh nodes ranked by connection likelihood.
This file must be executable (chmod +x) to work as a dynamic page.
"""
import json
import os
import time
import urllib.error
import urllib.request

# ── Configuration ────────────────────────────────────────────────────
# Adjust these if your dashboard runs on a different port or host.
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8080
MAX_NODES = 30
FETCH_TIMEOUT = 5  # seconds

# ── Helpers ──────────────────────────────────────────────────────────

def fetch_api(path):
    """Fetch JSON from the local ReticulumPi dashboard API."""
    url = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}{path}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "ReticulumPi-NomadNet/1.0"}
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fmt_age(seconds):
    """Format seconds as compact duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    h = s // 3600
    m = (s % 3600) // 60
    if s < 86400:
        return f"{h}h{m}m" if m else f"{h}h"
    d = s // 86400
    h_rem = (s % 86400) // 3600
    return f"{d}d{h_rem}h" if h_rem else f"{d}d"


def score_bar(score, width=5):
    """Render score as ASCII bar."""
    filled = round(score / 100 * width)
    return "#" * filled + "-" * (width - filled)


def label_icon(label):
    """Return a short status marker for a reachability label."""
    return {
        "High": "[+]",
        "Good": "[+]",
        "Fair": "[~]",
        "Low": "[!]",
        "Unlikely": "[!]",
    }.get(label, "[?]")


def short_hash(h):
    """Shorten a hex hash for display."""
    clean = h.replace("<", "").replace(">", "").replace(" ", "")
    return clean[:12] if len(clean) > 12 else clean


def node_display_name(entry):
    """Get the best human-readable name for a node."""
    name = entry.get("app_data") or entry.get("app_name") or ""
    if name:
        # Truncate long names
        return name[:20] if len(name) > 20 else name
    return short_hash(entry.get("destination_hash", "???"))


# ── Render ───────────────────────────────────────────────────────────

now_str = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())

print("`!`F222`Bddd`cNetwork Reachability`!")
print("`c" + now_str)
print("-")
print("`a`b`f")

try:
    data = fetch_api("/api/reachability?limit=" + str(MAX_NODES))
    if not data.get("ok"):
        raise ValueError(data.get("error", "API returned error"))

    nodes = data["data"]["nodes"]
    summary = data["data"]["summary"]

    # ── Summary ──────────────────────────────────────────────────
    print(">Summary")
    print("")
    total = summary.get("total_scored", 0)
    print(f"  Nodes scored   : {total}")
    if total > 0:
        avg = summary.get("average_score", 0)
        high = summary.get("high", 0)
        good = summary.get("good", 0)
        fair = summary.get("fair", 0)
        low = summary.get("low", 0)
        unlikely = summary.get("unlikely", 0)
        print(f"  Average score  : {avg:.0f}/100")
        print("")
        print(f"  `!High`!     : {high}")
        print(f"  `!Good`!     : {good}")
        print(f"  `!Fair`!     : {fair}")
        print(f"  `!Low`!      : {low}")
        print(f"  `!Unlikely`! : {unlikely}")
    print("")
    print("-")

    # ── Node list ────────────────────────────────────────────────
    print(">Reachability Scores")
    print("")

    if not nodes:
        print("  No known nodes to score.")
    else:
        # Header
        print(f"  {'Score':>5}  {'Bar':5}  {'Node':<22}  {'Hops':>4}  {'Seen':>6}")
        print(f"  {'-----':>5}  {'-----':5}  {'----':<22}  {'----':>4}  {'----':>6}")

        for entry in nodes:
            score = entry.get("score", 0)
            label = entry.get("label", "?")
            bar = score_bar(score)
            name = node_display_name(entry)
            hops = entry.get("hops")
            hops_str = str(hops) if hops is not None else "?"

            last_seen = entry.get("last_seen")
            if last_seen:
                seen_str = fmt_age(time.time() - last_seen)
            else:
                seen_str = "?"

            icon = label_icon(label)
            print(f"  {score:>5}  {bar:5}  {icon} {name:<18}  {hops_str:>4}  {seen_str:>6}")

        shown = len(nodes)
        if shown < total:
            print(f"\n  (Showing top {shown} of {total} nodes)")

    print("")
    print("-")

    # ── Scoring guide ────────────────────────────────────────────
    print(">Score Guide")
    print("")
    print("  80-100  `!High`!      Very likely to connect")
    print("   60-79  `!Good`!      Probably reachable")
    print("   40-59  `!Fair`!      May need path refresh")
    print("   20-39  `!Low`!       Path likely expired")
    print("    0-19  `!Unlikely`!  No path or relay down")
    print("")
    print("  Factors: path existence (30), freshness (20),")
    print("           hops (15), announce age (15), relay (20)")

except urllib.error.HTTPError as exc:
    print(">Network Reachability")
    print("")
    if exc.code == 401 or exc.code == 403:
        print("  `!Dashboard requires authentication.`!")
        print("")
        print("  The reachability API needs the web dashboard")
        print("  to be running without auth, or with a token.")
        print(f"  (HTTP {exc.code})")
    else:
        print(f"  `!API error (HTTP {exc.code})`!")
    print("")

except urllib.error.URLError:
    print(">Network Reachability")
    print("")
    print("  `!Dashboard API unavailable.`!")
    print("")
    print("  Make sure the web_dashboard plugin is running.")
    print(f"  Expected at: http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    print("")

except Exception as exc:
    print(">Network Reachability")
    print("")
    print(f"  `!Error: {exc}`!")
    print("")

# ── Footer ───────────────────────────────────────────────────────
print("-")
viewer = os.environ.get("remote_identity", None)
if viewer:
    print(f"`c  Viewed by: {viewer[:16]}...")
else:
    print("`c  Viewed by: anonymous")
print("")
print("`cPowered by ReticulumPi")
print("`c`[`:/page/index.mu`Return to Home]")
