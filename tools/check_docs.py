#!/usr/bin/env python3
"""Validate documentation links, normative references, and CLI help snapshots."""

from __future__ import annotations

import argparse
import difflib
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

try:
    from tools.generate_docs_reference import REFERENCE_PATH, reference_diff, write_reference
except ModuleNotFoundError:  # Direct ``python tools/check_docs.py`` execution.
    from generate_docs_reference import REFERENCE_PATH, reference_diff, write_reference


ROOT = Path(__file__).resolve().parents[1]
ROOT_NORMATIVE_DOCUMENTS = {
    Path("CHANGELOG.md"),
    Path("CLAUDE.md"),
    Path("CONTRIBUTING.md"),
    Path("README.md"),
    Path("SECURITY.md"),
}
HISTORICAL_DOCUMENTS = {
    Path("AUDIT-2026-05-06.md"),
    Path("docs/github-discussion-link-routing.md"),
    Path("docs/reddit-post.md"),
    Path("docs/solar-power-build.md"),
    Path("docs/verification-6e4edc4.md"),
    Path("docs/wiki-node-entry.md"),
}
SNAPSHOT_COMMANDS = {
    Path("docs/cli-help/reticulumpi.txt"): ("reticulumpi.cli", "--help"),
    Path("docs/cli-help/reticulumpi-admin.txt"): (
        "reticulumpi.admin_cli",
        "--help",
    ),
}
MARKDOWN_LINK = re.compile(r"!?\[[^]]*\]\(([^)]+)\)")
AUDIT_ROW = re.compile(r"^\|\s*(P[123]-\d{2})\s*\|", re.MULTILINE)
EXPECTED_AUDIT_IDS = {
    *(f"P1-{number:02d}" for number in range(1, 22)),
    *(f"P2-{number:02d}" for number in range(1, 28)),
    *(f"P3-{number:02d}" for number in range(1, 5)),
}
STALE_RULES = (
    (
        "legacy virtualenv path; use <install-root>/current/.venv",
        re.compile(r"/opt/reticulumpi/\.venv(?:/|\b)"),
    ),
    (
        "source-checkout script path is not installed in immutable releases",
        re.compile(r"/(?:opt|srv)/reticulumpi/scripts/nomadnet-tui\.sh\b"),
    ),
    (
        "mutable MeshChat checkout inside the immutable install root",
        re.compile(r"/(?:opt|srv)/reticulumpi/meshchat(?:/|\b)"),
    ),
    (
        "production reference to legacy service home",
        re.compile(
            r"^(?!.*\blegacy\s+migration\s+input\b).*\/home\/reticulumpi(?:/|\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported transactional bootstrap feature",
        re.compile(r"--with-meshchat\b"),
    ),
    (
        "removed anonymous-localhost configuration",
        re.compile(r"\ballow_localhost_(?:api|send)\b", re.IGNORECASE),
    ),
    (
        "claim that localhost bypasses authentication",
        re.compile(
            r"\blocalhost\s+(?:requests?|clients?|api)\b[^\n]{0,60}"
            r"(?:\bbypass(?:es|ed)?\b[^\n]{0,20}\bauth|"
            r"\bwithout\s+auth|\bdo\s+not\s+require\s+auth)",
            re.IGNORECASE,
        ),
    ),
    (
        "claim that anonymous localhost access is enabled",
        re.compile(
            r"\banonymous\s+localhost\b[^\n]{0,60}\b(?:enabled|allowed)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "legacy file-transfer empty-allowlist open default",
        re.compile(
            r"allowed_identities[^\n]{0,50}\bempty\b[^\n]{0,30}"
            r"\baccept\s+from\s+anyone\b",
            re.IGNORECASE,
        ),
    ),
    (
        "obsolete dashboard-password location",
        re.compile(
            r"(?:(?:/etc|/var/lib|/opt)/reticulumpi/"
            r"|/home/reticulumpi/|(?:~|/home/reticulumpi)/\.reticulumpi/)"
            r"(?:dashboard[_-])?password(?:\.txt)?\b",
            re.IGNORECASE,
        ),
    ),
    (
        "obsolete claim that a generated password is logged",
        re.compile(
            r"\b(?:generated|auto-generated|dashboard)\s+password\b[^\n]{0,50}"
            r"(?:\blogged\s+once\b|\b(?:is|was|will be)\s+logged\b|"
            r"\bwritten\s+to\s+(?:the\s+)?journal\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "unsupported Python version; production starts at Python 3.11",
        re.compile(r"\bPython\s*3\.(?:8|9|10)\b", re.IGNORECASE),
    ),
    (
        "manual route/plugin/event count; link the generated code reference",
        re.compile(
            r"\b\d+\+?\s+(?:(?:REST|HTTP|WebSocket|public|built-in)\s+)*"
            r"(?:endpoints?|routes?|plugins?|event(?:\s+types?)?)\b",
            re.IGNORECASE,
        ),
    ),
)


def _relative(path: Path) -> Path:
    return path.resolve().relative_to(ROOT)


def _display(path: Path) -> Path:
    resolved = path.resolve()
    return _relative(resolved) if resolved.is_relative_to(ROOT) else resolved


def markdown_files() -> tuple[Path, ...]:
    """Return every repository Markdown document in deterministic order."""

    root_documents = ROOT_NORMATIVE_DOCUMENTS | {
        path for path in HISTORICAL_DOCUMENTS if path.parent == Path(".")
    }
    files = [*(ROOT / path for path in root_documents), *(ROOT / "docs").rglob("*.md")]
    return tuple(sorted(path.resolve() for path in files if path.is_file()))


def normative_text_files() -> tuple[Path, ...]:
    """Return normative docs and configuration examples subject to stale scans."""

    candidates = [
        *(ROOT / path for path in ROOT_NORMATIVE_DOCUMENTS),
        *(ROOT / "docs").rglob("*.md"),
        *(ROOT / "config").rglob("*.yaml"),
        *(ROOT / "config").rglob("*.yml"),
    ]
    return tuple(
        sorted(
            path.resolve()
            for path in candidates
            if path.is_file() and _relative(path) not in HISTORICAL_DOCUMENTS
        )
    )


def _link_target(raw: str) -> str:
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def check_local_links(files: tuple[Path, ...] | list[Path]) -> list[str]:
    """Return diagnostics for missing local inline Markdown links and images."""

    errors: list[str] = []
    for document in sorted(Path(path).resolve() for path in files):
        text = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(text):
            raw_target = _link_target(match.group(1))
            parsed = urlsplit(raw_target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            decoded = unquote(parsed.path)
            target = (
                ROOT / decoded.lstrip("/") if decoded.startswith("/") else document.parent / decoded
            )
            if not target.resolve().exists():
                line = text.count("\n", 0, match.start()) + 1
                errors.append(
                    f"{_display(document)}:{line}: missing local link target: {raw_target}"
                )
    return errors


def stale_reference_errors(path: Path, text: str) -> list[str]:
    """Return line-oriented diagnostics for stale normative references."""

    errors: list[str] = []
    display = _display(path)
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in STALE_RULES:
            if path.resolve() == REFERENCE_PATH.resolve() and label.startswith("manual route"):
                continue
            if pattern.search(line):
                errors.append(f"{display}:{line_number}: {label}: {line.strip()}")
    return errors


def check_stale_references(files: tuple[Path, ...] | list[Path]) -> list[str]:
    errors: list[str] = []
    for path in sorted(Path(item).resolve() for item in files):
        errors.extend(stale_reference_errors(path, path.read_text(encoding="utf-8")))
    return errors


def _capture_help(module: str, *arguments: str) -> str:
    environment = dict(os.environ)
    python_path = str(ROOT / "src")
    if environment.get("PYTHONPATH"):
        python_path += os.pathsep + environment["PYTHONPATH"]
    environment.update(
        {
            "COLUMNS": "80",
            "LANG": "C",
            "LC_ALL": "C",
            "PYTHONHASHSEED": "0",
            "PYTHONPATH": python_path,
            "PYTHONUTF8": "1",
            "TERM": "dumb",
        }
    )
    result = subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"{module} help failed: {detail}")
    if result.stderr.strip():
        raise RuntimeError(f"{module} help wrote to stderr: {result.stderr.strip()}")
    normalized = "\n".join(line.rstrip() for line in result.stdout.splitlines())
    return normalized.rstrip() + "\n"


def check_help_snapshots(*, refresh: bool = False) -> list[str]:
    """Compare deterministic CLI help with committed snapshots, or refresh them."""

    errors: list[str] = []
    for relative, command in sorted(SNAPSHOT_COMMANDS.items()):
        snapshot = ROOT / relative
        try:
            actual = _capture_help(*command)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        if refresh:
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_text(actual, encoding="utf-8")
            continue
        if not snapshot.is_file():
            errors.append(
                f"{relative}: help snapshot is missing; run "
                "`python tools/check_docs.py --refresh-help`"
            )
            continue
        expected = snapshot.read_text(encoding="utf-8")
        if expected != actual:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=str(relative),
                    tofile=f"current {command[0]} --help",
                )
            )
            errors.append(
                f"{relative}: CLI help changed; review it and refresh intentionally:\n{diff}"
            )
    return errors


def check_generated_reference(*, refresh: bool = False) -> list[str]:
    """Compare generated route/default/plugin/event docs with current code."""

    if refresh:
        write_reference()
        return []
    difference = reference_diff()
    if difference is None:
        return []
    relative = REFERENCE_PATH.relative_to(ROOT)
    return [
        f"{relative}: generated code reference is stale; review source changes and run "
        f"`python tools/generate_docs_reference.py`:\n{difference}"
    ]


def audit_ledger_errors(path: Path | None = None) -> list[str]:
    """Require the immutable 52-finding ledger ID set exactly once."""

    ledger = path or ROOT / "docs/audit-remediation-2026-07.md"
    if not ledger.is_file():
        return [f"{_display(ledger)}: audit remediation ledger is missing"]
    rows = AUDIT_ROW.findall(ledger.read_text(encoding="utf-8"))
    row_set = set(rows)
    errors: list[str] = []
    if len(rows) != 52:
        errors.append(f"{_display(ledger)}: expected exactly 52 audit rows, found {len(rows)}")
    duplicates = sorted(identifier for identifier in row_set if rows.count(identifier) > 1)
    if duplicates:
        errors.append(f"{_display(ledger)}: duplicate audit IDs: {', '.join(duplicates)}")
    missing = sorted(EXPECTED_AUDIT_IDS - row_set)
    unexpected = sorted(row_set - EXPECTED_AUDIT_IDS)
    if missing:
        errors.append(f"{_display(ledger)}: missing audit IDs: {', '.join(missing)}")
    if unexpected:
        errors.append(f"{_display(ledger)}: unexpected audit IDs: {', '.join(unexpected)}")
    return errors


def run_checks(*, refresh_help: bool = False, refresh_generated: bool = False) -> list[str]:
    errors = check_generated_reference(refresh=refresh_generated)
    errors.extend(check_local_links(markdown_files()))
    errors.extend(check_stale_references(normative_text_files()))
    errors.extend(check_help_snapshots(refresh=refresh_help))
    errors.extend(audit_ledger_errors())
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-help",
        action="store_true",
        help="rewrite CLI help snapshots after reviewing an intentional CLI change",
    )
    parser.add_argument(
        "--refresh-generated",
        action="store_true",
        help="rewrite the source-derived route/default/plugin/event reference",
    )
    arguments = parser.parse_args(argv)
    errors = run_checks(
        refresh_help=arguments.refresh_help,
        refresh_generated=arguments.refresh_generated,
    )
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    action = "refreshed" if arguments.refresh_help or arguments.refresh_generated else "verified"
    print(
        "Documentation links, generated references, normative references, audit ledger, "
        f"and CLI help snapshots {action}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
