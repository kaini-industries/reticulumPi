"""CLI entry point for reticulumPi."""

from __future__ import annotations

import argparse
import logging
import sys

from reticulumpi import __version__
from reticulumpi.app import ReticulumPiApp

# Map RNS log levels (0-7) to Python logging levels
_RNS_TO_LOGGING = {
    0: logging.CRITICAL,
    1: logging.ERROR,
    2: logging.WARNING,
    3: logging.WARNING,
    4: logging.INFO,
    5: logging.DEBUG,
    6: logging.DEBUG,
    7: logging.DEBUG,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reticulumpi",
        description="ReticulumPi - An extensible Reticulum network node",
    )
    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config",
        "-c",
        default=None,
        help="Path to reticulumPi config YAML (default: ~/.config/reticulumpi/config.yaml)",
    )
    parser.add_argument(
        "--reticulum-config",
        default=None,
        help="Path to Reticulum config directory (overrides config file setting)",
    )
    parser.add_argument(
        "--log-level",
        type=int,
        default=None,
        choices=range(0, 8),
        metavar="0-7",
        help="Log level: 0=critical, 1=error, 2-3=warning, 4=info, 5-7=debug (overrides config)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        default=False,
        help="Validate configuration and plugin discovery without starting (dry run)",
    )
    parser.add_argument(
        "--list-plugins",
        action="store_true",
        default=False,
        help="List all discoverable plugins and exit",
    )
    parser.add_argument(
        "--hash-password",
        action="store_true",
        default=False,
        help="Generate a password hash for the web_dashboard plugin and exit",
    )
    parser.add_argument(
        "--backup-identity",
        metavar="PATH",
        default=None,
        help="Back up the node identity file to PATH and exit",
    )
    parser.add_argument(
        "--restore-identity",
        metavar="PATH",
        default=None,
        help="Restore the node identity file from PATH and exit",
    )
    parser.add_argument(
        "--log-format",
        choices=["text", "json"],
        default=None,
        help="Log output format (overrides config; default: text)",
    )
    parser.add_argument(
        "--remote",
        metavar="HASH",
        default=None,
        help="Connect to a remote ReticulumPi node via Reticulum Link for management",
    )
    parser.add_argument(
        "--command",
        default=None,
        help="Execute a single remote command (use with --remote). Without this, opens interactive shell.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Timeout in seconds for remote operations (default: 30)",
    )
    parser.add_argument(
        "--mesh-bridge",
        choices=["status", "pause", "resume"],
        default=None,
        help="Control the Meshtastic↔MeshCore bridge via the local dashboard API. "
        "Requires RETICULUMPI_DASHBOARD_PASSWORD env var or prompts for it.",
    )
    args = parser.parse_args()

    if args.mesh_bridge:
        _run_mesh_bridge_cli(args.mesh_bridge, args.config)
        sys.exit(0)

    if args.hash_password:
        import getpass
        from reticulumpi.builtin_plugins.web_dashboard.auth import hash_password

        pw = getpass.getpass("Enter dashboard password: ")
        if not pw:
            print("Error: password cannot be empty")
            sys.exit(1)
        pw2 = getpass.getpass("Confirm password: ")
        if pw != pw2:
            print("Error: passwords do not match")
            sys.exit(1)
        print()
        print("Add this to your config.yaml under plugins.web_dashboard:")
        print(f'  password_hash: "{hash_password(pw)}"')
        sys.exit(0)

    if args.remote:
        from reticulumpi.remote_client import RemoteClient, run_interactive, run_single_command

        reticulum_config = args.reticulum_config
        client = RemoteClient(
            destination_hex=args.remote,
            reticulum_config_dir=reticulum_config,
            timeout=args.timeout,
        )
        if not client.connect():
            sys.exit(1)
        try:
            if args.command:
                parts = args.command.strip().split(None, 1)
                cmd = parts[0]
                cmd_args = parts[1] if len(parts) > 1 else ""
                rc = run_single_command(client, cmd, cmd_args)
                sys.exit(rc)
            else:
                run_interactive(client)
        finally:
            client.close()
        sys.exit(0)

    if args.backup_identity:
        import os
        import shutil

        config_path_tmp = args.config
        if config_path_tmp is None:
            default_path = os.path.expanduser("~/.config/reticulumpi/config.yaml")
            if os.path.isfile(default_path):
                config_path_tmp = default_path
        tmp_app = ReticulumPiApp(config_path=config_path_tmp)
        src = tmp_app.config.identity_path
        if not os.path.isfile(src):
            print(f"Error: identity file not found at {src}")
            sys.exit(1)
        dst = os.path.expanduser(args.backup_identity)
        shutil.copy2(src, dst)
        print(f"Identity backed up: {src} -> {dst}")
        sys.exit(0)

    if args.restore_identity:
        import os
        import shutil

        config_path_tmp = args.config
        if config_path_tmp is None:
            default_path = os.path.expanduser("~/.config/reticulumpi/config.yaml")
            if os.path.isfile(default_path):
                config_path_tmp = default_path
        tmp_app = ReticulumPiApp(config_path=config_path_tmp)
        src = os.path.expanduser(args.restore_identity)
        if not os.path.isfile(src):
            print(f"Error: backup file not found at {src}")
            sys.exit(1)
        # Validate the backup is a loadable RNS identity
        try:
            import RNS

            test_id = RNS.Identity.from_file(src)
            if test_id is None:
                raise ValueError("Identity.from_file returned None")
        except Exception as e:
            print(f"Error: invalid identity file: {e}")
            sys.exit(1)
        dst = tmp_app.config.identity_path
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        print(f"Identity restored: {src} -> {dst}")
        sys.exit(0)

    config_path = args.config
    if config_path is None:
        import os

        default_path = os.path.expanduser("~/.config/reticulumpi/config.yaml")
        if os.path.isfile(default_path):
            config_path = default_path

    app = ReticulumPiApp(
        config_path=config_path,
        reticulum_config_dir=args.reticulum_config,
        log_level_override=args.log_level,
    )

    # Determine log level: CLI flag > config file > default (4=info)
    rns_level = args.log_level if args.log_level is not None else app.config.log_level
    python_level = _RNS_TO_LOGGING.get(rns_level, logging.INFO)

    log_format = args.log_format or app.config._data.get("log_format", "text")
    if log_format == "json":
        import json as _json
        import time as _time

        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return _json.dumps(
                    {
                        "ts": _time.strftime("%Y-%m-%dT%H:%M:%S", _time.localtime(record.created)),
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": record.getMessage(),
                    }
                )

        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
        logging.root.addHandler(handler)
        logging.root.setLevel(python_level)
    else:
        logging.basicConfig(
            level=python_level,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    if args.check:
        success = app.check()
        sys.exit(0 if success else 1)

    if args.list_plugins:
        app.list_plugins()
        sys.exit(0)

    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    except Exception:
        logging.exception("Fatal error in ReticulumPi")
        sys.exit(1)


def _run_mesh_bridge_cli(action: str, config_path: str | None) -> None:
    """Call the dashboard API to control the mesh_bridge plugin.

    Reads dashboard host/port from the config file and authenticates via
    the RETICULUMPI_DASHBOARD_PASSWORD env var (or prompts).
    """
    import getpass
    import json as _json
    import os
    import urllib.error
    import urllib.request

    if config_path is None:
        default_path = os.path.expanduser("~/.config/reticulumpi/config.yaml")
        if os.path.isfile(default_path):
            config_path = default_path

    # Always connect to localhost — this is a local operator tool, so the
    # host field from config (which may be an external bind address like
    # 0.0.0.0 or 192.168.x.x) is ignored.  This prevents sending the
    # dashboard password over the network in plaintext when SSL is off.
    port = 8080
    scheme = "http"
    if config_path and os.path.isfile(config_path):
        try:
            import yaml

            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            wd = (cfg.get("plugins", {}) or {}).get("web_dashboard", {}) or {}
            port = int(wd.get("port", port))
            if (wd.get("ssl") or {}).get("enabled"):
                scheme = "https"
        except Exception:
            pass

    base = f"{scheme}://127.0.0.1:{port}"

    password = os.environ.get("RETICULUMPI_DASHBOARD_PASSWORD")
    if not password:
        password = getpass.getpass("Dashboard password: ")
    if not password:
        print("Error: password required", file=sys.stderr)
        sys.exit(1)

    try:
        login_req = urllib.request.Request(
            f"{base}/api/auth/login",
            data=_json.dumps({"password": password}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(login_req, timeout=10) as resp:
            login_body = _json.loads(resp.read())
        token = (login_body.get("data") or {}).get("token")
        if not token:
            print("Error: login failed", file=sys.stderr)
            sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Error: cannot reach dashboard at {base}: {exc}", file=sys.stderr)
        sys.exit(1)

    if action == "status":
        url = f"{base}/api/mesh_bridge/status"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}"},
        )
    else:
        url = f"{base}/api/mesh_bridge/running"
        body = _json.dumps({"running": action == "resume"}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"Error: HTTP {exc.code}: {exc.read().decode('utf-8')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
