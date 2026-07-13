#!/usr/bin/env python3
"""Enforce changed-code and critical-module coverage policy.

The changed-line calculation compares the current worktree with a supplied Git
base commit.  Only executable lines reported by coverage.py are counted, so a
change containing only comments or whitespace does not dilute the result.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path("src/reticulumpi")
REQUIRED_PERCENT = 90
DEFAULT_RELEASE_VERSION = "0.3.2"
_EMPTY_TREE_BASE = "reticulumpi:empty-tree"
CRITICAL_MODULES = (
    Path("src/reticulumpi/identity_manager.py"),
    Path("src/reticulumpi/builtin_plugins/web_dashboard/auth.py"),
    Path("src/reticulumpi/app.py"),
    Path("src/reticulumpi/plugin_base.py"),
    Path("src/reticulumpi/migrations.py"),
    Path("src/reticulumpi/admin_cli.py"),
)

_BRANCH_COVERAGE = re.compile(r"^\s*\d+(?:\.\d+)?%\s*\((\d+)/(\d+)\)\s*$")
_HUNK_HEADER = re.compile(rb"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
_HEX_OBJECT_ID = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_RELEASE_VERSION = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:/")


class CoverageGateError(RuntimeError):
    """Raised when coverage inputs cannot be evaluated safely."""


@dataclass(frozen=True)
class FileCoverage:
    """Executable-line and branch coverage for one repository file."""

    path: Path
    executable_lines: frozenset[int]
    covered_lines: frozenset[int]
    covered_branches: int
    total_branches: int

    @property
    def line_percent(self) -> float:
        return _percent(len(self.covered_lines), len(self.executable_lines))

    @property
    def branch_percent(self) -> float:
        return _percent(self.covered_branches, self.total_branches)


@dataclass(frozen=True)
class CoverageReport:
    """Normalized coverage.py XML contents keyed by repository path."""

    files: dict[Path, FileCoverage]


@dataclass(frozen=True)
class ChangedFile:
    """A current production Python file and its changed destination lines."""

    path: Path
    lines: frozenset[int]
    status: str
    previous_path: Path | None = None


@dataclass(frozen=True)
class ChangeSet:
    """Current changed files plus deleted production Python paths."""

    files: tuple[ChangedFile, ...]
    deleted: tuple[Path, ...]
    base_commit: str | None = None


@dataclass(frozen=True)
class CoveragePolicy:
    """Aggregate coverage requirements for one release milestone."""

    release_version: str
    line_percent: int
    branch_percent: int | None


@dataclass(frozen=True)
class AggregateResult:
    """Aggregate coverage counts evaluated against a release policy."""

    covered_lines: int
    total_lines: int
    covered_branches: int
    total_branches: int
    policy: CoveragePolicy

    @property
    def line_percent(self) -> float:
        return _percent(self.covered_lines, self.total_lines)

    @property
    def branch_percent(self) -> float:
        return _percent(self.covered_branches, self.total_branches)

    @property
    def line_passed(self) -> bool:
        return _meets_threshold(self.covered_lines, self.total_lines, self.policy.line_percent)

    @property
    def branch_passed(self) -> bool:
        minimum = self.policy.branch_percent
        return minimum is None or _meets_threshold(
            self.covered_branches, self.total_branches, minimum
        )

    @property
    def passed(self) -> bool:
        return self.line_passed and self.branch_passed


@dataclass(frozen=True)
class ModuleResult:
    """Coverage result for one critical module."""

    path: Path
    covered_lines: int
    total_lines: int
    covered_branches: int
    total_branches: int
    passed: bool
    missing: bool = False

    @property
    def line_percent(self) -> float:
        return _percent(self.covered_lines, self.total_lines)

    @property
    def branch_percent(self) -> float:
        return _percent(self.covered_branches, self.total_branches)


@dataclass(frozen=True)
class GateResult:
    """Aggregate policy decision and diagnostics."""

    changed_covered: int
    changed_total: int
    unmeasured_changed: tuple[Path, ...]
    critical_modules: tuple[ModuleResult, ...]
    failures: tuple[str, ...]
    aggregate: AggregateResult | None = None

    @property
    def changed_percent(self) -> float:
        return _percent(self.changed_covered, self.changed_total)

    @property
    def passed(self) -> bool:
        return not self.failures


def _percent(covered: int, total: int) -> float:
    """Return a coverage percentage, treating an empty denominator as complete."""

    return 100.0 if total == 0 else covered * 100.0 / total


def _meets_threshold(covered: int, total: int, threshold: int = REQUIRED_PERCENT) -> bool:
    """Compare integer counts without floating-point rounding."""

    return total == 0 or covered * 100 >= threshold * total


def coverage_policy_for_release(raw_version: str) -> CoveragePolicy:
    """Return the locked aggregate coverage policy for a release version."""

    match = _RELEASE_VERSION.fullmatch(raw_version.strip())
    if match is None:
        raise CoverageGateError(f"release version must be vMAJOR.MINOR.PATCH, got {raw_version!r}")
    version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    canonical = ".".join(str(component) for component in version)
    if version >= (0, 3, 1):
        return CoveragePolicy(canonical, line_percent=70, branch_percent=60)
    if version >= (0, 3, 0):
        return CoveragePolicy(canonical, line_percent=65, branch_percent=55)
    return CoveragePolicy(canonical, line_percent=50, branch_percent=None)


def _display(path: Path) -> str:
    return path.as_posix()


def _lexical_relative_path(raw: str, *, label: str) -> Path:
    """Normalize an untrusted repository path without allowing traversal."""

    value = raw.replace("\\", "/")
    if (
        not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or _WINDOWS_ABSOLUTE.match(value)
    ):
        raise CoverageGateError(f"invalid {label}: {raw!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise CoverageGateError(f"unsafe {label}: {raw!r}")
    return Path(*pure.parts)


def _relative_to_repo(candidate: Path, repo_root: Path, *, label: str) -> Path:
    resolved = candidate.resolve(strict=False)
    try:
        normalized = resolved.relative_to(repo_root)
    except ValueError as exc:
        raise CoverageGateError(f"{label} escapes repository root: {candidate}") from exc
    try:
        lexical = candidate.absolute().relative_to(repo_root)
    except ValueError as exc:
        raise CoverageGateError(
            f"{label} uses a non-canonical repository path: {candidate}"
        ) from exc
    if lexical != normalized:
        raise CoverageGateError(f"{label} resolves through a symlink: {candidate}")
    return normalized


def _coverage_sources(root: ET.Element, repo_root: Path) -> tuple[Path, ...]:
    sources = {repo_root}
    for element in root.findall("./sources/source"):
        if element.text is None or not element.text.strip():
            continue
        raw = element.text.strip().replace("\\", "/")
        if _WINDOWS_ABSOLUTE.match(raw):
            raise CoverageGateError(f"unsupported coverage source path: {element.text!r}")
        candidate = Path(raw)
        if any(ord(character) < 32 or ord(character) == 127 for character in raw) or (
            candidate.is_absolute() and ".." in candidate.parts
        ):
            raise CoverageGateError(f"unsafe coverage source path: {element.text!r}")
        if not candidate.is_absolute():
            relative = _lexical_relative_path(raw, label="coverage source")
            candidate = repo_root / relative
        resolved = candidate.resolve(strict=False)
        _relative_to_repo(candidate, repo_root, label="coverage source")
        if not resolved.is_dir():
            raise CoverageGateError(f"coverage source does not exist: {candidate}")
        sources.add(resolved)
    return tuple(sorted(sources, key=lambda path: path.as_posix()))


def _coverage_file_path(raw: str, repo_root: Path, sources: tuple[Path, ...]) -> Path:
    value = raw.replace("\\", "/")
    if _WINDOWS_ABSOLUTE.match(value):
        raise CoverageGateError(f"unsupported absolute coverage filename: {raw!r}")

    filename = Path(value)
    if filename.is_absolute():
        if any(ord(character) < 32 or ord(character) == 127 for character in value) or (
            ".." in filename.parts
        ):
            raise CoverageGateError(f"unsafe absolute coverage filename: {raw!r}")
        candidates = {filename}
    else:
        relative = _lexical_relative_path(value, label="coverage filename")
        candidates = {source / relative for source in sources}

    normalized: dict[Path, Path] = {}
    for candidate in candidates:
        relative = _relative_to_repo(candidate, repo_root, label="coverage filename")
        if candidate.is_file():
            normalized[relative] = candidate

    if not normalized:
        raise CoverageGateError(f"coverage filename does not exist in repository: {raw!r}")
    if len(normalized) != 1:
        paths = ", ".join(sorted(_display(path) for path in normalized))
        raise CoverageGateError(f"ambiguous coverage filename {raw!r}: {paths}")
    return next(iter(normalized))


def _parse_nonnegative_integer(raw: str | None, *, label: str) -> int:
    if raw is None or not raw.isdecimal():
        raise CoverageGateError(f"invalid {label}: {raw!r}")
    value = int(raw)
    return value


def parse_coverage_xml(path: Path, repo_root: Path) -> CoverageReport:
    """Parse coverage.py XML and normalize every filename to the repository."""

    repo_root = repo_root.resolve(strict=True)
    if not repo_root.is_dir():
        raise CoverageGateError(f"repository root is not a directory: {repo_root}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CoverageGateError(f"cannot read coverage XML {path}: {exc}") from exc
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise CoverageGateError("coverage XML must not contain DTD or entity declarations")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CoverageGateError(f"malformed coverage XML: {exc}") from exc
    if root.tag != "coverage":
        raise CoverageGateError(f"unexpected coverage XML root: {root.tag!r}")

    sources = _coverage_sources(root, repo_root)
    classes = root.findall("./packages/package/classes/class")
    if not classes:
        raise CoverageGateError("coverage XML contains no class records")

    files: dict[Path, FileCoverage] = {}
    for class_element in classes:
        raw_filename = class_element.get("filename")
        if raw_filename is None:
            raise CoverageGateError("coverage class is missing its filename")
        file_path = _coverage_file_path(raw_filename, repo_root, sources)
        if file_path in files:
            raise CoverageGateError(f"duplicate coverage record: {_display(file_path)}")

        executable: set[int] = set()
        covered: set[int] = set()
        covered_branches = 0
        total_branches = 0
        line_elements = class_element.findall("./lines/line")
        for line_element in line_elements:
            number = _parse_nonnegative_integer(
                line_element.get("number"), label=f"line number in {_display(file_path)}"
            )
            if number == 0 or number in executable:
                raise CoverageGateError(
                    f"invalid or duplicate line number {number} in {_display(file_path)}"
                )
            hits = _parse_nonnegative_integer(
                line_element.get("hits"), label=f"hit count in {_display(file_path)}:{number}"
            )
            executable.add(number)
            if hits > 0:
                covered.add(number)

            branch = line_element.get("branch", "false").lower()
            if branch not in {"true", "false"}:
                raise CoverageGateError(
                    f"invalid branch flag in {_display(file_path)}:{number}: {branch!r}"
                )
            if branch == "true":
                condition = line_element.get("condition-coverage", "")
                match = _BRANCH_COVERAGE.fullmatch(condition)
                if match is None:
                    raise CoverageGateError(
                        f"invalid branch coverage in {_display(file_path)}:{number}: {condition!r}"
                    )
                line_covered, line_total = (int(value) for value in match.groups())
                if line_total <= 0 or line_covered > line_total:
                    raise CoverageGateError(
                        f"invalid branch counts in {_display(file_path)}:{number}: "
                        f"{line_covered}/{line_total}"
                    )
                covered_branches += line_covered
                total_branches += line_total

        files[file_path] = FileCoverage(
            path=file_path,
            executable_lines=frozenset(executable),
            covered_lines=frozenset(covered),
            covered_branches=covered_branches,
            total_branches=total_branches,
        )
    return CoverageReport(files=files)


def _run_git(repo_root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise CoverageGateError(f"could not execute Git: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise CoverageGateError(
            f"git {' '.join(arguments[:2])} failed: {detail or f'exit status {result.returncode}'}"
        )
    return result.stdout


def _base_commit(repo_root: Path, base_revision: str) -> str:
    if (
        not base_revision
        or base_revision.startswith("-")
        or any(character in base_revision for character in "\x00\r\n")
    ):
        raise CoverageGateError(f"invalid Git base revision: {base_revision!r}")
    try:
        output = _run_git(
            repo_root,
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{base_revision}^{{commit}}",
        )
    except CoverageGateError as exc:
        raise CoverageGateError(
            f"Git base revision does not resolve to a commit: {base_revision!r}"
        ) from exc
    try:
        commit = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CoverageGateError("Git returned a non-ASCII base commit identifier") from exc
    if not _HEX_OBJECT_ID.fullmatch(commit):
        raise CoverageGateError(f"Git base did not resolve to a commit: {base_revision!r}")
    return commit


def _empty_tree(repo_root: Path) -> str:
    """Return the repository-format empty-tree object ID."""

    output = _run_git(repo_root, "hash-object", "-t", "tree", "/dev/null")
    try:
        object_id = output.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise CoverageGateError("Git returned a non-ASCII empty-tree identifier") from exc
    if not _HEX_OBJECT_ID.fullmatch(object_id):
        raise CoverageGateError("Git did not return a valid empty-tree identifier")
    return object_id


def _release_version_tuple(tag: str) -> tuple[int, int, int]:
    if not tag.startswith("v"):
        raise CoverageGateError(f"release tag must be vMAJOR.MINOR.PATCH, got {tag!r}")
    match = _RELEASE_VERSION.fullmatch(tag)
    if match is None:
        raise CoverageGateError(f"release tag must be vMAJOR.MINOR.PATCH, got {tag!r}")
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def _previous_release_base(repo_root: Path, ref: str, current_commit: str) -> str:
    """Return the prior reachable release commit, or the empty-tree sentinel."""

    current_tag = ref.removeprefix("refs/tags/")
    current_version = _release_version_tuple(current_tag)
    if _base_commit(repo_root, current_tag) != current_commit:
        raise CoverageGateError(f"release tag {current_tag!r} does not identify checked-out HEAD")
    try:
        raw_tags = _run_git(repo_root, "tag", "--merged", "HEAD", "--list", "v*").decode(
            "utf-8", errors="strict"
        )
    except UnicodeDecodeError as exc:
        raise CoverageGateError("Git returned a non-UTF-8 release tag") from exc
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for tag in raw_tags.splitlines():
        match = _RELEASE_VERSION.fullmatch(tag)
        if match is None or not tag.startswith("v"):
            continue
        version = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
        if version < current_version:
            candidates.append((version, tag))
    if not candidates:
        return _EMPTY_TREE_BASE
    _version, previous_tag = max(candidates)
    return _base_commit(repo_root, previous_tag)


def _event_string(payload: dict[str, object], *keys: str) -> str:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise CoverageGateError(f"GitHub event is missing required field: {'.'.join(keys)}")
        current = current[key]
    if not isinstance(current, str) or not current:
        raise CoverageGateError(f"invalid GitHub event field: {'.'.join(keys)}")
    return current


def _is_zero_object_id(value: str) -> bool:
    return len(value) in {40, 64} and set(value) == {"0"}


def resolve_github_base(repo_root: Path, event_name: str, event_path: Path) -> str:
    """Derive and verify the changed-code base for a GitHub Actions event."""

    try:
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CoverageGateError(f"cannot read GitHub event payload {event_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CoverageGateError("GitHub event payload must be a JSON object")

    if event_name == "pull_request":
        base = _event_string(payload, "pull_request", "base", "sha")
        return _base_commit(repo_root, base)

    if event_name != "push":
        raise CoverageGateError(f"unsupported GitHub event for coverage base: {event_name!r}")

    ref = _event_string(payload, "ref")
    if not ref.startswith(("refs/heads/", "refs/tags/")):
        raise CoverageGateError(f"unsupported push ref for coverage base: {ref!r}")
    if payload.get("deleted") is True:
        raise CoverageGateError("cannot establish coverage base for a deleted ref")

    current = _base_commit(repo_root, "HEAD")
    after = _event_string(payload, "after")
    if _is_zero_object_id(after):
        raise CoverageGateError("push event has an all-zero destination commit")
    after_commit = _base_commit(repo_root, after)
    if after_commit != current:
        raise CoverageGateError(
            f"push event destination {after_commit} does not match checked-out HEAD {current}"
        )

    if ref.startswith("refs/tags/"):
        return _previous_release_base(repo_root, ref, current)

    before = _event_string(payload, "before")
    if not _is_zero_object_id(before):
        return _base_commit(repo_root, before)

    if payload.get("created") is not True:
        raise CoverageGateError(
            "push event has an all-zero base but does not identify a newly created ref"
        )
    try:
        return _base_commit(repo_root, "HEAD^")
    except CoverageGateError as exc:
        raise CoverageGateError(
            "initial push has no parent commit; changed-code coverage cannot establish a base"
        ) from exc


def _decode_git_path(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CoverageGateError("Git path is not valid UTF-8") from exc


def _git_path(raw: bytes, repo_root: Path, *, must_exist: bool) -> Path:
    relative = _lexical_relative_path(_decode_git_path(raw), label="Git path")
    candidate = repo_root / relative
    normalized = _relative_to_repo(candidate, repo_root, label="Git path")
    if must_exist and not candidate.is_file():
        raise CoverageGateError(f"changed path is not a regular file: {_display(normalized)}")
    return normalized


def _is_production_python(path: Path) -> bool:
    return path.suffix == ".py" and (path == SOURCE_ROOT or SOURCE_ROOT in path.parents)


def _name_status_records(
    data: bytes, repo_root: Path
) -> tuple[list[tuple[str, Path | None, Path]], list[Path]]:
    if data and not data.endswith(b"\0"):
        raise CoverageGateError("unterminated Git name-status output")
    tokens = data.split(b"\0")[:-1] if data else []
    records: list[tuple[str, Path | None, Path]] = []
    deleted: list[Path] = []
    index = 0
    while index < len(tokens):
        try:
            status_text = tokens[index].decode("ascii", errors="strict")
        except UnicodeDecodeError as exc:
            raise CoverageGateError("invalid Git change status") from exc
        index += 1
        if not status_text:
            raise CoverageGateError("empty Git change status")
        status = status_text[0]
        if status not in {"A", "C", "D", "M", "R", "T", "U", "X", "B"}:
            raise CoverageGateError(f"unsupported Git change status: {status_text!r}")
        if status in {"C", "R"}:
            score = status_text[1:]
            if not score.isdecimal() or not 0 <= int(score) <= 100:
                raise CoverageGateError(f"invalid Git similarity status: {status_text!r}")
        elif status_text != status:
            raise CoverageGateError(f"invalid Git change status: {status_text!r}")
        path_count = 2 if status in {"C", "R"} else 1
        if index + path_count > len(tokens):
            raise CoverageGateError(f"truncated Git record for status {status_text!r}")
        if status in {"C", "R"}:
            previous = _git_path(tokens[index], repo_root, must_exist=False)
            current = _git_path(tokens[index + 1], repo_root, must_exist=True)
        else:
            previous = None
            current = _git_path(tokens[index], repo_root, must_exist=status != "D")
        index += path_count

        if status in {"U", "X", "B"}:
            raise CoverageGateError(
                f"cannot evaluate unresolved Git status {status_text!r} for {_display(current)}"
            )
        if status == "D":
            if _is_production_python(current):
                deleted.append(current)
            continue
        if _is_production_python(current):
            records.append((status, previous, current))
    return records, deleted


def _changed_lines_from_patch(patch: bytes, path: Path) -> frozenset[int]:
    if b"GIT binary patch" in patch or b"Binary files " in patch:
        raise CoverageGateError(f"cannot calculate Python line coverage for binary file: {path}")
    changed: set[int] = set()
    for line in patch.splitlines():
        match = _HUNK_HEADER.match(line)
        if match is None:
            continue
        start = int(match.group("start"))
        raw_count = match.group("count")
        count = 1 if raw_count is None else int(raw_count)
        if count > 0:
            changed.update(range(start, start + count))
    return frozenset(changed)


def collect_changed_python(repo_root: Path, base_revision: str) -> ChangeSet:
    """Return production Python destination lines changed since *base_revision*."""

    repo_root = repo_root.resolve(strict=True)
    commit = (
        _empty_tree(repo_root)
        if base_revision == _EMPTY_TREE_BASE
        else _base_commit(repo_root, base_revision)
    )
    name_status = _run_git(
        repo_root,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--no-ext-diff",
        "--no-textconv",
        commit,
        "--",
    )
    records, deleted = _name_status_records(name_status, repo_root)

    changed: dict[Path, ChangedFile] = {}
    for status, previous, current in records:
        path_arguments = [current.as_posix()]
        if status == "R" and previous is not None:
            path_arguments.insert(0, previous.as_posix())
        patch = _run_git(
            repo_root,
            "diff",
            "--unified=0",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            commit,
            "--",
            *path_arguments,
        )
        changed[current] = ChangedFile(
            path=current,
            lines=_changed_lines_from_patch(patch, current),
            status=status,
            previous_path=previous,
        )

    untracked = _run_git(
        repo_root,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        SOURCE_ROOT.as_posix(),
    )
    if untracked and not untracked.endswith(b"\0"):
        raise CoverageGateError("unterminated Git untracked-file output")
    for raw_path in untracked.split(b"\0")[:-1] if untracked else ():
        current = _git_path(raw_path, repo_root, must_exist=True)
        if not _is_production_python(current):
            continue
        if current in changed:
            raise CoverageGateError(f"duplicate changed Git path: {_display(current)}")
        try:
            line_count = len((repo_root / current).read_text(encoding="utf-8").splitlines())
        except (OSError, UnicodeError) as exc:
            raise CoverageGateError(f"cannot read changed Python file {current}: {exc}") from exc
        changed[current] = ChangedFile(
            path=current,
            lines=frozenset(range(1, line_count + 1)),
            status="?",
        )

    return ChangeSet(
        files=tuple(changed[path] for path in sorted(changed, key=lambda item: item.as_posix())),
        deleted=tuple(sorted(set(deleted), key=lambda item: item.as_posix())),
        base_commit=commit,
    )


def evaluate_coverage(
    report: CoverageReport,
    changes: ChangeSet,
    *,
    critical_modules: Sequence[Path] = CRITICAL_MODULES,
    threshold: int = REQUIRED_PERCENT,
    policy: CoveragePolicy | None = None,
) -> GateResult:
    """Evaluate changed executable lines and critical modules against policy."""

    if not 1 <= threshold <= 100:
        raise ValueError("coverage threshold must be between 1 and 100")
    if policy is None:
        policy = coverage_policy_for_release(DEFAULT_RELEASE_VERSION)

    changed_covered = 0
    changed_total = 0
    unmeasured_changed: list[Path] = []
    failures: list[str] = []
    aggregate = AggregateResult(
        covered_lines=sum(len(item.covered_lines) for item in report.files.values()),
        total_lines=sum(len(item.executable_lines) for item in report.files.values()),
        covered_branches=sum(item.covered_branches for item in report.files.values()),
        total_branches=sum(item.total_branches for item in report.files.values()),
        policy=policy,
    )
    if not aggregate.line_passed:
        failures.append(
            f"aggregate line coverage is below {policy.line_percent}% for "
            f"{policy.release_version}: {aggregate.covered_lines}/{aggregate.total_lines} "
            f"({aggregate.line_percent:.2f}%)"
        )
    if not aggregate.branch_passed:
        failures.append(
            f"aggregate branch coverage is below {policy.branch_percent}% for "
            f"{policy.release_version}: {aggregate.covered_branches}/{aggregate.total_branches} "
            f"({aggregate.branch_percent:.2f}%)"
        )

    for changed_file in changes.files:
        if not changed_file.lines:
            continue
        coverage = report.files.get(changed_file.path)
        if coverage is None:
            unmeasured_changed.append(changed_file.path)
            failures.append(
                f"changed production file is missing from coverage XML: "
                f"{_display(changed_file.path)}"
            )
            continue
        executable_changed = changed_file.lines & coverage.executable_lines
        changed_total += len(executable_changed)
        changed_covered += len(executable_changed & coverage.covered_lines)

    if not _meets_threshold(changed_covered, changed_total, threshold):
        failures.append(
            f"changed executable lines are below {threshold}%: "
            f"{changed_covered}/{changed_total} ({_percent(changed_covered, changed_total):.2f}%)"
        )

    module_results: list[ModuleResult] = []
    for module in critical_modules:
        normalized = Path(module)
        coverage = report.files.get(normalized)
        if coverage is None:
            module_results.append(
                ModuleResult(
                    path=normalized,
                    covered_lines=0,
                    total_lines=0,
                    covered_branches=0,
                    total_branches=0,
                    passed=False,
                    missing=True,
                )
            )
            failures.append(f"critical module is missing from coverage XML: {_display(normalized)}")
            continue

        line_passed = _meets_threshold(
            len(coverage.covered_lines), len(coverage.executable_lines), threshold
        )
        branch_passed = _meets_threshold(
            coverage.covered_branches, coverage.total_branches, threshold
        )
        passed = line_passed and branch_passed
        module_results.append(
            ModuleResult(
                path=normalized,
                covered_lines=len(coverage.covered_lines),
                total_lines=len(coverage.executable_lines),
                covered_branches=coverage.covered_branches,
                total_branches=coverage.total_branches,
                passed=passed,
            )
        )
        if not passed:
            failures.append(
                f"critical module is below {threshold}%: {_display(normalized)} "
                f"(line {_percent(len(coverage.covered_lines), len(coverage.executable_lines)):.2f}%, "
                f"branch {_percent(coverage.covered_branches, coverage.total_branches):.2f}%)"
            )

    return GateResult(
        changed_covered=changed_covered,
        changed_total=changed_total,
        unmeasured_changed=tuple(unmeasured_changed),
        critical_modules=tuple(module_results),
        failures=tuple(failures),
        aggregate=aggregate,
    )


def _format_result(result: GateResult) -> str:
    changed_passed = not result.unmeasured_changed and _meets_threshold(
        result.changed_covered, result.changed_total
    )
    changed_label = "PASS" if changed_passed else "FAIL"
    if result.unmeasured_changed:
        changed_detail = (
            f"coverage data missing for {len(result.unmeasured_changed)} changed file(s)"
        )
    elif result.changed_total == 0:
        changed_detail = "no executable production lines changed"
    else:
        changed_detail = (
            f"{result.changed_covered}/{result.changed_total} ({result.changed_percent:.2f}%)"
        )
    lines: list[str] = []
    if result.aggregate is not None:
        aggregate = result.aggregate
        line_label = "PASS" if aggregate.line_passed else "FAIL"
        branch_label = (
            "REPORTED"
            if aggregate.policy.branch_percent is None
            else ("PASS" if aggregate.branch_passed else "FAIL")
        )
        branch_requirement = (
            "no minimum"
            if aggregate.policy.branch_percent is None
            else f"minimum {aggregate.policy.branch_percent}%"
        )
        lines.extend(
            [
                f"Aggregate coverage ({aggregate.policy.release_version} policy):",
                f"  line {aggregate.covered_lines}/{aggregate.total_lines} "
                f"({aggregate.line_percent:.2f}%, minimum "
                f"{aggregate.policy.line_percent}%) [{line_label}]",
                f"  branch {aggregate.covered_branches}/{aggregate.total_branches} "
                f"({aggregate.branch_percent:.2f}%, {branch_requirement}) [{branch_label}]",
            ]
        )
    lines.extend(
        [f"Changed-code coverage: {changed_detail} [{changed_label}]", "Critical modules:"]
    )
    for module in result.critical_modules:
        if module.missing:
            lines.append(f"  FAIL {_display(module.path)}: missing from coverage XML")
            continue
        label = "PASS" if module.passed else "FAIL"
        lines.append(
            f"  {label} {_display(module.path)}: "
            f"line {module.covered_lines}/{module.total_lines} ({module.line_percent:.2f}%), "
            f"branch {module.covered_branches}/{module.total_branches} "
            f"({module.branch_percent:.2f}%)"
        )
    if result.failures:
        lines.extend(["Failures:", *(f"  - {failure}" for failure in result.failures)])
    lines.append("Coverage gate passed." if result.passed else "Coverage gate failed.")
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Enforce release-specific aggregate coverage plus 90% coverage of changed "
            "executable ReticulumPi lines and 90% line/branch coverage for every "
            "critical module."
        ),
    )
    parser.add_argument(
        "base",
        nargs="?",
        help=(
            "Git base revision to compare with the current worktree; omit when using --github-event"
        ),
    )
    parser.add_argument(
        "--coverage-xml",
        type=Path,
        default=Path("coverage.xml"),
        help="coverage.py XML report (default: coverage.xml)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=ROOT,
        help="Git repository root (default: repository containing this tool)",
    )
    parser.add_argument(
        "--release-version",
        default=DEFAULT_RELEASE_VERSION,
        help=(
            "release policy version: 0.2.5 defaults to 50%% line; 0.3.0 requires "
            "65%% line/55%% branch; 0.3.1+ requires 70%% line/60%% branch "
            f"(default: {DEFAULT_RELEASE_VERSION})"
        ),
    )
    parser.add_argument(
        "--github-event",
        type=Path,
        help="derive the base SHA from this GitHub Actions event JSON instead of BASE",
    )
    parser.add_argument(
        "--github-event-name",
        choices=("pull_request", "push"),
        help="GitHub Actions event name used with --github-event",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        repo_root = args.repo_root.resolve(strict=True)
        policy = coverage_policy_for_release(args.release_version)
        if args.github_event is not None:
            if args.base is not None:
                raise CoverageGateError("BASE and --github-event are mutually exclusive")
            if args.github_event_name is None:
                raise CoverageGateError("--github-event-name is required with --github-event")
            base = resolve_github_base(repo_root, args.github_event_name, args.github_event)
        else:
            if args.base is None:
                raise CoverageGateError("BASE is required unless --github-event is used")
            if args.github_event_name is not None:
                raise CoverageGateError("--github-event-name requires --github-event")
            base = args.base
        report = parse_coverage_xml(args.coverage_xml, repo_root)
        changes = collect_changed_python(repo_root, base)
        result = evaluate_coverage(report, changes, policy=policy)
    except (CoverageGateError, OSError) as exc:
        print(f"Coverage gate input error: {exc}", file=sys.stderr)
        return 2
    print(f"Changed-code base: {changes.base_commit}")
    print(_format_result(result))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
