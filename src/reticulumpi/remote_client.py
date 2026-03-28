"""Remote control client — connect to a ReticulumPi node over Reticulum and issue commands."""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

import RNS
import RNS.vendor.umsgpack as umsgpack


# Commands that don't require extra input
SIMPLE_COMMANDS = {
    "ping": "/ping",
    "status": "/status",
    "metrics": "/metrics",
    "plugins": "/plugins",
    "interfaces": "/interfaces",
    "config": "/config",
    "logs": "/logs",
    "announce": "/announce",
}


class RemoteClient:
    """Establishes an RNS Link to a remote ReticulumPi node and issues commands."""

    def __init__(
        self,
        destination_hex: str,
        reticulum_config_dir: str | None = None,
        identity_path: str | None = None,
        timeout: float = 30.0,
    ):
        self._destination_hex = destination_hex.replace("<", "").replace(">", "").replace(" ", "")
        self._timeout = timeout
        self._link: Any = None
        self._link_ready = threading.Event()
        self._link_closed = threading.Event()

        # Initialize Reticulum
        self.reticulum = RNS.Reticulum(configdir=reticulum_config_dir)

        # Load or create client identity
        if identity_path:
            import os
            if os.path.isfile(identity_path):
                self.identity = RNS.Identity.from_file(identity_path)
            else:
                self.identity = RNS.Identity()
                self.identity.to_file(identity_path)
        else:
            self.identity = RNS.Identity()

    def connect(self) -> bool:
        """Establish a Link to the remote node and identify.

        Returns True if connection and identification succeed.
        """
        try:
            dest_hash = bytes.fromhex(self._destination_hex)
        except ValueError:
            print(f"Error: invalid destination hash: {self._destination_hex}")
            return False

        # Resolve the destination
        if not RNS.Transport.has_path(dest_hash):
            print(f"Requesting path to {RNS.prettyhexrep(dest_hash)}...")
            RNS.Transport.request_path(dest_hash)
            # Wait for path
            deadline = time.time() + self._timeout
            while not RNS.Transport.has_path(dest_hash):
                if time.time() > deadline:
                    print("Error: path request timed out")
                    return False
                time.sleep(0.5)

        # Create the destination
        remote_identity = RNS.Identity.recall(dest_hash)
        destination = RNS.Destination(
            remote_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            "reticulumpi",
            "node",
            "control",
        )

        # Establish Link
        print(f"Connecting to {RNS.prettyhexrep(dest_hash)}...")
        self._link = RNS.Link(destination, established_callback=self._link_established)
        self._link.set_link_closed_callback(self._on_link_closed)

        # Wait for link establishment
        remaining = self._timeout
        while not self._link_ready.is_set():
            if self._link_closed.is_set():
                print("Error: link was closed before establishment")
                return False
            if not self._link_ready.wait(timeout=min(0.25, remaining)):
                remaining -= 0.25
                if remaining <= 0:
                    print("Error: link establishment timed out")
                    return False

        # Identify ourselves
        self._link.identify(self.identity)

        # Give the remote side time to process identification
        time.sleep(1.0)

        if self._link_closed.is_set():
            print("Error: link closed after identification (likely unauthorized)")
            return False

        print(f"Connected to {RNS.prettyhexrep(dest_hash)}")
        return True

    def request(self, path: str, data: Any = None, timeout: float | None = None) -> dict[str, Any] | None:
        """Send a request over the Link and return the parsed response."""
        if not self._link or self._link_closed.is_set():
            return None

        if timeout is None:
            timeout = self._timeout

        request_data = None
        if data is not None:
            request_data = umsgpack.packb(data)

        receipt = self._link.request(path, data=request_data, timeout=timeout)

        # Wait for response
        deadline = time.time() + timeout
        while receipt.response is None and not receipt.timed_out:
            if time.time() > deadline:
                break
            time.sleep(0.25)

        if receipt.timed_out or receipt.response is None:
            return None

        try:
            return umsgpack.unpackb(receipt.response)
        except Exception:
            return {"raw": receipt.response}

    def close(self) -> None:
        """Tear down the link."""
        if self._link and not self._link_closed.is_set():
            try:
                self._link.teardown()
            except Exception:
                pass

    def _link_established(self, link: Any) -> None:
        self._link_ready.set()

    def _on_link_closed(self, link: Any) -> None:
        self._link_closed.set()


def _format_response(data: Any, indent: int = 0) -> str:
    """Format a response dict or list for terminal display."""
    if isinstance(data, list):
        lines = []
        prefix = "  " * indent
        for item in data:
            if isinstance(item, dict):
                lines.append(_format_response(item, indent))
                lines.append("")
            else:
                lines.append(f"{prefix}- {item}")
        return "\n".join(lines)
    if not isinstance(data, dict):
        return str(data)
    lines = []
    prefix = "  " * indent
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(_format_response(value, indent + 1))
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(_format_response(item, indent + 1))
                    lines.append("")
                else:
                    lines.append(f"{prefix}  - {item}")
        else:
            lines.append(f"{prefix}{key}: {value}")
    return "\n".join(lines)


def run_interactive(client: RemoteClient) -> None:
    """Run an interactive command shell."""
    print()
    print("Remote control shell. Type 'help' for commands, 'quit' to exit.")
    print()

    while True:
        try:
            raw = input("remote> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            _print_help()
            continue

        if cmd in SIMPLE_COMMANDS:
            path = SIMPLE_COMMANDS[cmd]
            data = None
            if cmd == "logs" and args:
                try:
                    data = {"count": int(args)}
                except ValueError:
                    print(f"Invalid log count: {args}")
                    continue

            print(f"Requesting {path}...")
            resp = client.request(path, data=data)
            if resp is None:
                print("Error: request timed out or link closed")
                continue

            if not resp.get("ok", False):
                print(f"Error: {resp.get('error', 'unknown')}")
                continue

            if cmd == "ping":
                print(f"Pong from {resp.get('node', '?')} at {time.ctime(resp.get('time', 0))}")
            elif "data" in resp:
                print(_format_response(resp["data"]))
            else:
                print(_format_response(resp))

        elif cmd == "enable":
            if not args:
                print("Usage: enable <plugin_name>")
                continue
            resp = client.request("/plugin/enable", {"name": args})
            if resp is None:
                print("Error: request timed out")
            elif resp.get("ok"):
                print(resp.get("message", "Plugin enabled"))
            else:
                print(f"Error: {resp.get('error', 'unknown')}")

        elif cmd == "disable":
            if not args:
                print("Usage: disable <plugin_name>")
                continue
            resp = client.request("/plugin/disable", {"name": args})
            if resp is None:
                print("Error: request timed out")
            elif resp.get("ok"):
                print(resp.get("message", "Plugin disabled"))
            else:
                print(f"Error: {resp.get('error', 'unknown')}")

        else:
            print(f"Unknown command: {cmd}. Type 'help' for available commands.")


def _print_help() -> None:
    print("""
Available commands:
  ping          — Check connectivity and round-trip time
  status        — Get node status (version, plugins, identity)
  metrics       — Get system metrics (CPU, temp, memory, disk)
  plugins       — List all running plugins with status
  interfaces    — Show Reticulum interface details
  config        — View node configuration (sensitive values stripped)
  logs [N]      — View last N log lines (default: 100)
  announce      — Trigger an immediate heartbeat announce
  enable NAME   — Hot-load and start a plugin
  disable NAME  — Stop and unload a plugin
  help          — Show this help
  quit          — Disconnect and exit
""".strip())


def run_single_command(client: RemoteClient, command: str, args: str = "") -> int:
    """Execute a single command and return exit code."""
    if command in SIMPLE_COMMANDS:
        path = SIMPLE_COMMANDS[command]
        data = None
        if command == "logs" and args:
            try:
                data = {"count": int(args)}
            except ValueError:
                print(f"Invalid log count: {args}")
                return 1

        resp = client.request(path, data=data)
        if resp is None:
            print("Error: request timed out or link closed")
            return 1

        if not resp.get("ok", False):
            print(f"Error: {resp.get('error', 'unknown')}")
            return 1

        if command == "ping":
            print(f"Pong from {resp.get('node', '?')} at {time.ctime(resp.get('time', 0))}")
        elif "data" in resp:
            print(_format_response(resp["data"]))
        else:
            print(_format_response(resp))
        return 0

    elif command == "enable":
        if not args:
            print("Usage: --remote HASH --command 'enable plugin_name'")
            return 1
        resp = client.request("/plugin/enable", {"name": args})
        if resp and resp.get("ok"):
            print(resp.get("message", "Plugin enabled"))
            return 0
        print(f"Error: {resp.get('error', 'unknown') if resp else 'timed out'}")
        return 1

    elif command == "disable":
        if not args:
            print("Usage: --remote HASH --command 'disable plugin_name'")
            return 1
        resp = client.request("/plugin/disable", {"name": args})
        if resp and resp.get("ok"):
            print(resp.get("message", "Plugin disabled"))
            return 0
        print(f"Error: {resp.get('error', 'unknown') if resp else 'timed out'}")
        return 1

    else:
        print(f"Unknown command: {command}")
        return 1
