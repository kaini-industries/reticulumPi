"""CLI entry point for reticulumPi."""

import argparse
import logging
import sys

from reticulumpi.app import ReticulumPiApp


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="reticulumpi",
        description="ReticulumPi - An extensible Reticulum network node",
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
        help="Log level 0-7 (overrides config file setting)",
    )
    args = parser.parse_args()

    config_path = args.config
    if config_path is None:
        import os
        default_path = os.path.expanduser("~/.config/reticulumpi/config.yaml")
        if os.path.isfile(default_path):
            config_path = default_path

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    app = ReticulumPiApp(
        config_path=config_path,
        reticulum_config_dir=args.reticulum_config,
    )

    try:
        app.start()
    except KeyboardInterrupt:
        app.shutdown()
    except Exception:
        logging.exception("Fatal error in ReticulumPi")
        sys.exit(1)


if __name__ == "__main__":
    main()
