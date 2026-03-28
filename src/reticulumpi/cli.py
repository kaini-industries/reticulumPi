"""CLI entry point for reticulumPi."""

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
        "--version", "-V",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--config", "-c",
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
    args = parser.parse_args()

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
        print(f"  password_hash: \"{hash_password(pw)}\"")
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


if __name__ == "__main__":
    main()
