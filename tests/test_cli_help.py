"""Regression tests for version-independent public CLI help formatting."""

from __future__ import annotations

import argparse

from reticulumpi.cli_help import STABLE_HELP_WIDTH, StableHelpFormatter


def test_stable_help_formatter_uses_reviewed_width() -> None:
    parser = argparse.ArgumentParser(prog="reticulumpi-admin", formatter_class=StableHelpFormatter)
    subcommands = parser.add_subparsers(dest="command")
    for command in (
        "install",
        "upgrade",
        "rollback",
        "status",
        "doctor",
        "external-artifact",
        "db",
    ):
        subcommands.add_parser(command)

    usage = parser.format_help().splitlines()[0]

    assert STABLE_HELP_WIDTH == 100
    assert len(usage) <= STABLE_HELP_WIDTH
    assert usage == (
        "usage: reticulumpi-admin [-h] "
        "{install,upgrade,rollback,status,doctor,external-artifact,db} ..."
    )
