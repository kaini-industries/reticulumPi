"""Stable argparse formatting shared by public command-line interfaces."""

from __future__ import annotations

import argparse


class StableHelpFormatter(argparse.HelpFormatter):
    """Keep optional-argument rendering stable across supported Python versions."""

    def _format_action_invocation(self, action: argparse.Action) -> str:
        if not action.option_strings:
            return super()._format_action_invocation(action)
        if action.nargs == 0:
            return ", ".join(action.option_strings)
        default = self._get_default_metavar_for_optional(action)
        arguments = self._format_args(action, default)
        return ", ".join(f"{option} {arguments}" for option in action.option_strings)
