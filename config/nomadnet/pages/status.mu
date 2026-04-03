#!/opt/reticulumpi/.venv/bin/python3
# -*- coding: utf-8 -*-
"""Dynamic NomadNet page: Node Status

Displays real-time system and Reticulum network status.
This file must be executable (chmod +x) to work as a dynamic page.
"""
import os
import subprocess
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fmt_bytes(n):
    """Format byte count as human-readable string."""
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_duration(seconds):
    """Format seconds as human-readable duration."""
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, secs = divmod(seconds, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def bar(pct, width=20):
    """Render a simple ASCII progress bar."""
    filled = int(round(pct / 100 * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


# ---------------------------------------------------------------------------
# System stats (read directly from /proc and /sys — instant, no deps)
# ---------------------------------------------------------------------------

def get_uptime():
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except Exception:
        return None


def get_load():
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            return parts[0], parts[1], parts[2]
    except Exception:
        return None, None, None


def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000
    except Exception:
        return None


def get_memory():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k.strip()] = int(v.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        used = total - avail
        pct = (used / total * 100) if total else 0
        return total * 1024, used * 1024, pct
    except Exception:
        return None, None, None


def get_disk():
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = total - free
        pct = (used / total * 100) if total else 0
        return total, used, pct
    except Exception:
        return None, None, None


def get_cpu_count():
    try:
        count = 0
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("processor"):
                    count += 1
        return count
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Reticulum network stats (via rnstatus subprocess)
# ---------------------------------------------------------------------------

def get_rnstatus():
    """Run rnstatus and parse the output into structured data."""
    venv_bin = "/opt/reticulumpi/.venv/bin"
    try:
        result = subprocess.run(
            [os.path.join(venv_bin, "rnstatus")],
            capture_output=True, text=True, timeout=10,
            env={"PATH": venv_bin + ":/usr/bin:/bin",
                 "HOME": os.path.expanduser("~")},
        )
        return result.stdout if result.returncode == 0 else None
    except Exception:
        return None


def parse_rnstatus(raw):
    """Extract interfaces and transport info from rnstatus output."""
    interfaces = []
    transport_hash = None
    rns_uptime = None
    current = None

    for line in raw.splitlines():
        stripped = line.strip()

        # Interface header lines (indented with name[...])
        if not stripped.startswith("Status") and "[" in stripped and "]" in stripped:
            # Save previous interface
            if current:
                interfaces.append(current)
            # Extract name — e.g. "TCPInterface[TCP Client beleth/...]"
            name = stripped
            current = {"name": name, "status": "?", "traffic_up": "", "traffic_down": ""}
            continue

        if current:
            if stripped.startswith("Status"):
                current["status"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Peers"):
                current["peers"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Clients"):
                current["clients"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Mode"):
                current["mode"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Traffic"):
                current["traffic_up"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("\u2193") or stripped.startswith("↓"):
                current["traffic_down"] = stripped.strip()

        # Transport line
        if "Transport Instance" in stripped:
            # Extract hash between < >
            start = stripped.find("<")
            end = stripped.find(">")
            if start >= 0 and end > start:
                transport_hash = stripped[start + 1:end]

        if "Uptime is" in stripped:
            rns_uptime = stripped.split("Uptime is", 1)[1].strip()

    # Don't forget last interface
    if current:
        interfaces.append(current)

    return interfaces, transport_hash, rns_uptime


# ---------------------------------------------------------------------------
# Render the page
# ---------------------------------------------------------------------------

now_str = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime())

# --- Header ---
print("`!`F222`Bddd`cReticulumPi Node Status`!")
print("`c" + now_str)
print("-")
print("`a`b`f")

# --- System Info ---
print(">System")
print("")

uptime = get_uptime()
load1, load5, load15 = get_load()
temp = get_cpu_temp()
mem_total, mem_used, mem_pct = get_memory()
disk_total, disk_used, disk_pct = get_disk()
cpus = get_cpu_count()

hostname = "?"
try:
    with open("/etc/hostname") as f:
        hostname = f.read().strip()
except Exception:
    pass

print(f"  Hostname   : {hostname}")
if uptime is not None:
    print(f"  Uptime     : {fmt_duration(uptime)}")
if cpus:
    print(f"  CPU        : {cpus} cores")
if load1:
    print(f"  Load       : {load1} / {load5} / {load15}  (1m / 5m / 15m)")
if temp is not None:
    temp_warn = "  !!!" if temp > 80 else ""
    print(f"  CPU Temp   : {temp:.1f} C{temp_warn}")

print("")

if mem_total is not None:
    print(f"  Memory     : {fmt_bytes(mem_used)} / {fmt_bytes(mem_total)}  ({mem_pct:.0f}%)")
    print(f"               {bar(mem_pct)}")
if disk_total is not None:
    print(f"  Disk       : {fmt_bytes(disk_used)} / {fmt_bytes(disk_total)}  ({disk_pct:.0f}%)")
    print(f"               {bar(disk_pct)}")

print("")
print("-")

# --- Reticulum Network ---
print(">Reticulum Network")
print("")

raw = get_rnstatus()
if raw:
    interfaces, transport_hash, rns_uptime = parse_rnstatus(raw)

    if transport_hash:
        print(f"  Transport  : <{transport_hash}>")
    if rns_uptime:
        print(f"  RNS Uptime : {rns_uptime}")
    print("")

    # Filter out internal interfaces
    visible = [i for i in interfaces
               if "LocalInterface" not in i["name"]
               and "Shared Instance" not in i["name"]]

    if visible:
        print("  `!Interfaces`!")
        print("")
        for iface in visible:
            status_icon = "+" if iface["status"] == "Up" else "!"
            print(f"  [{status_icon}] {iface['name']}")
            extras = []
            if "mode" in iface:
                extras.append(f"Mode: {iface['mode']}")
            if "peers" in iface:
                extras.append(f"Peers: {iface['peers']}")
            if "clients" in iface:
                extras.append(f"Clients: {iface['clients']}")
            if extras:
                print(f"      {' | '.join(extras)}")
            if iface.get("traffic_up"):
                print(f"      {iface['traffic_up']}")
            if iface.get("traffic_down"):
                print(f"      {iface['traffic_down']}")
            print("")
    else:
        print("  No external interfaces found.")
else:
    print("  `!Network status unavailable`!")
    print("  (rnstatus did not respond)")

print("-")

# --- Footer ---
print("")
print("`cPowered by ReticulumPi")
print("`c`[`:/page/index.mu`Return to Home]")
