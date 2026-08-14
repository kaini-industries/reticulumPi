"""Regression tests for the changed-code and critical-module coverage gate."""

from __future__ import annotations

import subprocess
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

import tools.check_coverage_gate as coverage_gate
from tools.check_coverage_gate import (
    CRITICAL_MODULES,
    ChangeSet,
    ChangedFile,
    CoverageGateError,
    CoveragePolicy,
    CoverageReport,
    FileCoverage,
    GateResult,
    ModuleResult,
    _base_commit,
    _changed_lines_from_patch,
    _format_result,
    _git_path,
    _lexical_relative_path,
    _name_status_records,
    _run_git,
    collect_changed_python,
    coverage_policy_for_release,
    evaluate_coverage,
    main,
    parse_coverage_xml,
    resolve_github_base,
)


def _file_coverage(
    path: str,
    *,
    executable: set[int] | None = None,
    covered: set[int] | None = None,
    branches: tuple[int, int] = (0, 0),
) -> FileCoverage:
    executable = executable if executable is not None else {1}
    covered = covered if covered is not None else set(executable)
    return FileCoverage(
        path=Path(path),
        executable_lines=frozenset(executable),
        covered_lines=frozenset(covered),
        covered_branches=branches[0],
        total_branches=branches[1],
    )


def _write_coverage_xml(
    repo: Path,
    records: dict[str, list[tuple[int, int, str | None]]],
    *,
    source: str | None = None,
) -> Path:
    coverage = ET.Element("coverage")
    sources = ET.SubElement(coverage, "sources")
    ET.SubElement(sources, "source").text = source or str(repo)
    packages = ET.SubElement(coverage, "packages")
    package = ET.SubElement(packages, "package", name="reticulumpi")
    classes = ET.SubElement(package, "classes")
    for filename, lines in records.items():
        class_element = ET.SubElement(classes, "class", filename=filename)
        line_elements = ET.SubElement(class_element, "lines")
        for number, hits, condition in lines:
            attributes = {"number": str(number), "hits": str(hits)}
            if condition is not None:
                attributes.update(branch="true", **{"condition-coverage": condition})
            ET.SubElement(line_elements, "line", **attributes)
    path = repo / "coverage.xml"
    ET.ElementTree(coverage).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> str:
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "coverage@example.invalid")
    _git(repo, "config", "user.name", "Coverage Gate")
    (repo / "src/reticulumpi").mkdir(parents=True)
    return ""


def _commit(repo: Path, message: str = "baseline") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_event(repo: Path, payload: object) -> Path:
    path = repo / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _bootstrap_release_repo(
    tmp_path: Path,
    *,
    historical_version: str = "0.2.4",
    release_tag: str = "v0.3.2",
) -> tuple[Path, str, str, Path]:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "reticulumpi"\nversion = "{historical_version}"\n',
        encoding="utf-8",
    )
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    baseline = _commit(repo, "historical version boundary")
    module.write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "release")
    _git(repo, "tag", release_tag, head)
    event = _write_event(
        repo,
        {
            "ref": f"refs/tags/{release_tag}",
            "before": "0" * 40,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )
    return repo, baseline, head, event


def test_parse_coverage_xml_normalizes_source_and_recomputes_counts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    source = repo / "src"
    module = source / "reticulumpi/app.py"
    module.parent.mkdir(parents=True)
    module.write_text("if ready:\n    run()\n", encoding="utf-8")
    xml = _write_coverage_xml(
        repo,
        {
            "reticulumpi/app.py": [
                (1, 1, "50% (1/2)"),
                (2, 0, None),
            ]
        },
        source=str(source),
    )

    report = parse_coverage_xml(xml, repo)

    parsed = report.files[Path("src/reticulumpi/app.py")]
    assert parsed.executable_lines == frozenset({1, 2})
    assert parsed.covered_lines == frozenset({1})
    assert parsed.covered_branches == 1
    assert parsed.total_branches == 2
    assert parsed.line_percent == 50.0
    assert parsed.branch_percent == 50.0


def test_parse_coverage_xml_accepts_normalized_backslashes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    module = repo / "src/reticulumpi/app.py"
    module.parent.mkdir(parents=True)
    module.write_text("ready = True\n", encoding="utf-8")
    xml = _write_coverage_xml(
        repo,
        {r"src\reticulumpi\app.py": [(1, 1, None)]},
    )

    report = parse_coverage_xml(xml, repo)

    assert set(report.files) == {Path("src/reticulumpi/app.py")}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"<coverage>", "malformed coverage XML"),
        (b"<not-coverage />", "unexpected coverage XML root"),
        (
            b'<!DOCTYPE coverage [<!ENTITY x "value">]><coverage>&x;</coverage>',
            "must not contain DTD",
        ),
    ],
)
def test_parse_coverage_xml_rejects_malformed_or_unsafe_xml(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    xml = repo / "coverage.xml"
    xml.write_bytes(payload)

    with pytest.raises(CoverageGateError, match=message):
        parse_coverage_xml(xml, repo)


@pytest.mark.parametrize("filename", ["../outside.py", "/tmp/outside.py", r"C:\outside.py"])
def test_parse_coverage_xml_rejects_paths_outside_repository(tmp_path: Path, filename: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    xml = _write_coverage_xml(repo, {filename: [(1, 1, None)]})

    with pytest.raises(CoverageGateError, match="coverage filename|absolute"):
        parse_coverage_xml(xml, repo)


def test_parse_coverage_xml_rejects_symlink_escape(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n", encoding="utf-8")
    (repo / "escape.py").symlink_to(outside)
    xml = _write_coverage_xml(repo, {"escape.py": [(1, 1, None)]})

    with pytest.raises(CoverageGateError, match="escapes repository root"):
        parse_coverage_xml(xml, repo)


def test_parse_coverage_xml_rejects_ambiguous_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "module.py").write_text("root = 1\n", encoding="utf-8")
    (repo / "src/module.py").write_text("source = 1\n", encoding="utf-8")
    xml = _write_coverage_xml(repo, {"module.py": [(1, 1, None)]}, source=str(repo / "src"))

    with pytest.raises(CoverageGateError, match="ambiguous coverage filename"):
        parse_coverage_xml(xml, repo)


def test_parse_coverage_xml_rejects_missing_duplicate_and_bad_branch_records(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    module = repo / "module.py"
    module.write_text("value = 1\n", encoding="utf-8")

    missing = _write_coverage_xml(repo, {"missing.py": [(1, 1, None)]})
    with pytest.raises(CoverageGateError, match="does not exist"):
        parse_coverage_xml(missing, repo)

    duplicate = repo / "duplicate.xml"
    duplicate.write_text(
        f"""<coverage><sources><source>{repo}</source></sources><packages><package>
        <classes><class filename="module.py"><lines><line number="1" hits="1"/></lines></class>
        <class filename="module.py"><lines><line number="1" hits="1"/></lines></class></classes>
        </package></packages></coverage>""",
        encoding="utf-8",
    )
    with pytest.raises(CoverageGateError, match="duplicate coverage record"):
        parse_coverage_xml(duplicate, repo)

    bad_branch = _write_coverage_xml(repo, {"module.py": [(1, 1, "not-a-count")]})
    with pytest.raises(CoverageGateError, match="invalid branch coverage"):
        parse_coverage_xml(bad_branch, repo)


@pytest.mark.parametrize(
    "raw", ["", "bad\0path.py", "bad\npath.py", "../escape.py", "/absolute.py", "C:/file.py"]
)
def test_lexical_path_normalization_rejects_unsafe_values(raw: str) -> None:
    with pytest.raises(CoverageGateError, match="invalid path|unsafe path"):
        _lexical_relative_path(raw, label="path")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("C:/outside", "unsupported coverage source"),
        ("missing", "coverage source does not exist"),
        ("..", "unsafe coverage source"),
    ],
)
def test_parse_coverage_xml_rejects_invalid_sources(
    tmp_path: Path, source: str, message: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("value = 1\n", encoding="utf-8")
    xml = _write_coverage_xml(repo, {"module.py": [(1, 1, None)]}, source=source)

    with pytest.raises(CoverageGateError, match=message):
        parse_coverage_xml(xml, repo)


def test_parse_coverage_xml_supports_relative_and_empty_source_entries(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    module = repo / "src/reticulumpi/app.py"
    module.parent.mkdir(parents=True)
    module.write_text("value = 1\n", encoding="utf-8")
    xml = repo / "coverage.xml"
    xml.write_text(
        """<coverage><sources><source></source><source>src</source></sources><packages>
        <package><classes><class filename="reticulumpi/app.py"><lines>
        <line number="1" hits="1"/></lines></class></classes></package>
        </packages></coverage>""",
        encoding="utf-8",
    )

    report = parse_coverage_xml(xml, repo)

    assert Path("src/reticulumpi/app.py") in report.files


def test_parse_coverage_xml_rejects_missing_inputs_and_invalid_records(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "module.py").write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(CoverageGateError, match="cannot read coverage XML"):
        parse_coverage_xml(repo / "missing.xml", repo)

    no_classes = repo / "no-classes.xml"
    no_classes.write_text("<coverage><packages/></coverage>", encoding="utf-8")
    with pytest.raises(CoverageGateError, match="no class records"):
        parse_coverage_xml(no_classes, repo)

    missing_filename = repo / "missing-filename.xml"
    missing_filename.write_text(
        "<coverage><packages><package><classes><class/></classes></package></packages></coverage>",
        encoding="utf-8",
    )
    with pytest.raises(CoverageGateError, match="missing its filename"):
        parse_coverage_xml(missing_filename, repo)

    invalid_lines = {
        "invalid hit count": '<line number="1"/>',
        "invalid or duplicate line number": '<line number="0" hits="1"/>',
        "invalid branch flag": '<line number="1" hits="1" branch="perhaps"/>',
        "invalid branch counts": (
            '<line number="1" hits="1" branch="true" condition-coverage="120% (3/2)"/>'
        ),
    }
    for message, line in invalid_lines.items():
        invalid = repo / f"invalid-{message.replace(' ', '-')}.xml"
        invalid.write_text(
            '<coverage><packages><package><classes><class filename="module.py"><lines>'
            f"{line}</lines></class></classes></package></packages></coverage>",
            encoding="utf-8",
        )
        with pytest.raises(CoverageGateError, match=message):
            parse_coverage_xml(invalid, repo)


def test_parse_coverage_xml_rejects_file_as_repository_root(tmp_path: Path) -> None:
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("content\n", encoding="utf-8")

    with pytest.raises(CoverageGateError, match="repository root is not a directory"):
        parse_coverage_xml(tmp_path / "coverage.xml", not_a_directory)


def test_parse_coverage_xml_rejects_in_repository_symlink_alias(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (repo / "alias.py").symlink_to(target)
    xml = _write_coverage_xml(repo, {"alias.py": [(1, 1, None)]})

    with pytest.raises(CoverageGateError, match="resolves through a symlink"):
        parse_coverage_xml(xml, repo)


@pytest.mark.parametrize(
    ("version", "line", "branch", "canonical"),
    [
        ("0.2.5", 50, None, "0.2.5"),
        ("v0.2.99", 50, None, "0.2.99"),
        ("v0.3.0", 65, 55, "0.3.0"),
        ("0.3.1", 70, 60, "0.3.1"),
        ("1.0.0", 70, 60, "1.0.0"),
    ],
)
def test_release_version_selects_locked_aggregate_policy(
    version: str, line: int, branch: int | None, canonical: str
) -> None:
    policy = coverage_policy_for_release(version)

    assert policy == CoveragePolicy(canonical, line_percent=line, branch_percent=branch)


@pytest.mark.parametrize("version", ["", "0.3", "v0.03.0", "0.3.1rc1", "release-0.3.1"])
def test_release_version_rejects_noncanonical_values(version: str) -> None:
    with pytest.raises(CoverageGateError, match="vMAJOR.MINOR.PATCH"):
        coverage_policy_for_release(version)


def test_aggregate_025_policy_enforces_line_and_reports_branch_without_minimum() -> None:
    path = Path("src/reticulumpi/module.py")
    report = CoverageReport(
        files={
            path: _file_coverage(
                str(path),
                executable=set(range(1, 11)),
                covered=set(range(1, 6)),
                branches=(1, 10),
            )
        }
    )

    result = evaluate_coverage(
        report,
        ChangeSet(files=(), deleted=()),
        critical_modules=(),
        policy=coverage_policy_for_release("0.2.5"),
    )

    assert result.passed
    assert result.aggregate is not None
    assert result.aggregate.line_percent == 50.0
    assert result.aggregate.branch_percent == 10.0
    rendered = _format_result(result)
    assert "line 5/10 (50.00%, minimum 50%) [PASS]" in rendered
    assert "branch 1/10 (10.00%, no minimum) [REPORTED]" in rendered


@pytest.mark.parametrize(
    ("version", "covered_lines", "covered_branches", "passed"),
    [
        ("0.3.0", 13, 11, True),
        ("0.3.0", 12, 11, False),
        ("0.3.0", 13, 10, False),
        ("0.3.1", 14, 12, True),
        ("0.3.1", 13, 12, False),
        ("0.3.1", 14, 11, False),
    ],
)
def test_aggregate_release_thresholds_are_enforced_exactly(
    version: str, covered_lines: int, covered_branches: int, passed: bool
) -> None:
    path = Path("src/reticulumpi/module.py")
    report = CoverageReport(
        files={
            path: _file_coverage(
                str(path),
                executable=set(range(1, 21)),
                covered=set(range(1, covered_lines + 1)),
                branches=(covered_branches, 20),
            )
        }
    )

    result = evaluate_coverage(
        report,
        ChangeSet(files=(), deleted=()),
        critical_modules=(),
        policy=coverage_policy_for_release(version),
    )

    assert result.passed is passed
    assert result.aggregate is not None
    assert result.aggregate.passed is passed
    if not passed:
        assert any(failure.startswith("aggregate ") for failure in result.failures)


def test_evaluate_coverage_requires_ninety_percent_changed_lines() -> None:
    path = Path("src/reticulumpi/feature.py")
    changes = ChangeSet(
        files=(ChangedFile(path=path, lines=frozenset(range(1, 11)), status="M"),),
        deleted=(),
    )

    passing = evaluate_coverage(
        CoverageReport(
            files={
                path: _file_coverage(
                    str(path), executable=set(range(1, 11)), covered=set(range(1, 10))
                )
            }
        ),
        changes,
        critical_modules=(),
    )
    failing = evaluate_coverage(
        CoverageReport(
            files={
                path: _file_coverage(
                    str(path), executable=set(range(1, 11)), covered=set(range(1, 9))
                )
            }
        ),
        changes,
        critical_modules=(),
    )

    assert passing.passed
    assert passing.changed_percent == 90.0
    assert not failing.passed
    assert failing.changed_percent == 80.0
    assert "changed executable lines are below 90%" in failing.failures[0]


def test_evaluate_coverage_counts_only_executable_changed_lines() -> None:
    path = Path("src/reticulumpi/feature.py")
    report = CoverageReport(files={path: _file_coverage(str(path), executable={2, 4}, covered={2})})
    changes = ChangeSet(
        files=(ChangedFile(path=path, lines=frozenset({1, 3, 5}), status="M"),),
        deleted=(),
    )

    result = evaluate_coverage(
        report,
        changes,
        critical_modules=(),
        policy=coverage_policy_for_release("0.2.5"),
    )

    assert result.passed
    assert result.changed_total == 0
    assert "no executable production lines changed" in _format_result(result)


def test_evaluate_coverage_fails_closed_for_unmeasured_changed_file() -> None:
    path = Path("src/reticulumpi/new_feature.py")
    changes = ChangeSet(
        files=(ChangedFile(path=path, lines=frozenset({1}), status="A"),),
        deleted=(),
    )

    result = evaluate_coverage(CoverageReport(files={}), changes, critical_modules=())

    assert not result.passed
    assert result.failures == (
        "changed production file is missing from coverage XML: src/reticulumpi/new_feature.py",
    )
    assert result.unmeasured_changed == (path,)
    assert "coverage data missing for 1 changed file(s) [FAIL]" in _format_result(result)


def test_evaluate_coverage_requires_line_and_branch_coverage_for_critical_modules() -> None:
    line_low = Path("src/reticulumpi/line_low.py")
    branch_low = Path("src/reticulumpi/branch_low.py")
    no_branches = Path("src/reticulumpi/no_branches.py")
    report = CoverageReport(
        files={
            line_low: _file_coverage(
                str(line_low), executable=set(range(10)), covered=set(range(8)), branches=(9, 10)
            ),
            branch_low: _file_coverage(
                str(branch_low), executable=set(range(10)), covered=set(range(9)), branches=(8, 10)
            ),
            no_branches: _file_coverage(
                str(no_branches), executable=set(range(10)), covered=set(range(9))
            ),
        }
    )

    result = evaluate_coverage(
        report,
        ChangeSet(files=(), deleted=()),
        critical_modules=(line_low, branch_low, no_branches),
    )

    assert not result.passed
    assert [module.passed for module in result.critical_modules] == [False, False, True]
    assert result.critical_modules[-1].branch_percent == 100.0
    assert any("line 80.00%, branch 90.00%" in failure for failure in result.failures)
    assert any("line 90.00%, branch 80.00%" in failure for failure in result.failures)


def test_evaluate_coverage_fails_when_critical_module_is_missing() -> None:
    missing = Path("src/reticulumpi/missing.py")

    result = evaluate_coverage(
        CoverageReport(files={}),
        ChangeSet(files=(), deleted=()),
        critical_modules=(missing,),
    )

    assert not result.passed
    assert result.critical_modules[0].missing
    assert "critical module is missing" in result.failures[0]


@pytest.mark.parametrize("threshold", [0, 101])
def test_evaluate_coverage_rejects_invalid_threshold(threshold: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 100"):
        evaluate_coverage(
            CoverageReport(files={}),
            ChangeSet(files=(), deleted=()),
            critical_modules=(),
            threshold=threshold,
        )


def test_format_result_renders_numeric_changed_coverage_and_missing_module() -> None:
    missing = Path("src/reticulumpi/missing.py")
    result = GateResult(
        changed_covered=8,
        changed_total=10,
        unmeasured_changed=(),
        critical_modules=(
            ModuleResult(
                path=missing,
                covered_lines=0,
                total_lines=0,
                covered_branches=0,
                total_branches=0,
                passed=False,
                missing=True,
            ),
        ),
        failures=("failure",),
    )

    rendered = _format_result(result)

    assert "8/10 (80.00%) [FAIL]" in rendered
    assert "FAIL src/reticulumpi/missing.py: missing from coverage XML" in rendered
    assert "  - failure" in rendered


def test_collect_changed_python_handles_modified_added_deleted_renamed_and_untracked(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source = repo / "src/reticulumpi"
    (source / "modified.py").write_text("value = 1\n", encoding="utf-8")
    (source / "deleted.py").write_text("obsolete = True\n", encoding="utf-8")
    (source / "old_name.py").write_text(
        "\n".join(["def one():", "    return 1", "", "def two():", "    return 2", ""]),
        encoding="utf-8",
    )
    base = _commit(repo)

    (source / "modified.py").write_text("value = 2\n", encoding="utf-8")
    (source / "deleted.py").unlink()
    (source / "old_name.py").rename(source / "new_name.py")
    renamed = source / "new_name.py"
    renamed.write_text(
        renamed.read_text(encoding="utf-8").replace("return 2", "return 3"), encoding="utf-8"
    )
    (source / "added.py").write_text("first = 1\nsecond = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    (source / "untracked.py").write_text("untracked = True\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_ignored.py").write_text("def test_it(): pass\n", encoding="utf-8")

    changes = collect_changed_python(repo, base)

    by_path = {changed.path: changed for changed in changes.files}
    assert set(by_path) == {
        Path("src/reticulumpi/added.py"),
        Path("src/reticulumpi/modified.py"),
        Path("src/reticulumpi/new_name.py"),
        Path("src/reticulumpi/untracked.py"),
    }
    assert by_path[Path("src/reticulumpi/modified.py")].lines == frozenset({1})
    assert by_path[Path("src/reticulumpi/added.py")].lines == frozenset({1, 2})
    assert by_path[Path("src/reticulumpi/new_name.py")].status == "R"
    assert by_path[Path("src/reticulumpi/new_name.py")].previous_path == Path(
        "src/reticulumpi/old_name.py"
    )
    assert by_path[Path("src/reticulumpi/new_name.py")].lines == frozenset({5})
    assert by_path[Path("src/reticulumpi/untracked.py")].status == "?"
    assert changes.deleted == (Path("src/reticulumpi/deleted.py"),)


def test_collect_changed_python_pure_rename_has_no_changed_destination_lines(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    source = repo / "src/reticulumpi"
    original = source / "original.py"
    original.write_text("value = 1\nother = 2\n", encoding="utf-8")
    base = _commit(repo)
    original.rename(source / "renamed.py")
    _git(repo, "add", "-A")

    changes = collect_changed_python(repo, base)

    assert changes.files == (
        ChangedFile(
            path=Path("src/reticulumpi/renamed.py"),
            lines=frozenset(),
            status="R",
            previous_path=Path("src/reticulumpi/original.py"),
        ),
    )


def test_collect_changed_python_rejects_missing_or_option_like_base(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src/reticulumpi/app.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repo)

    with pytest.raises(CoverageGateError, match="base revision does not resolve"):
        collect_changed_python(repo, "does-not-exist")
    with pytest.raises(CoverageGateError, match="invalid Git base revision"):
        collect_changed_python(repo, "--upload-pack=touch-danger")


def test_resolve_github_base_for_pull_request_and_normal_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    base = _commit(repo)
    module.write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "head")

    pull_request = _write_event(repo, {"pull_request": {"base": {"sha": base}}})
    assert resolve_github_base(repo, "pull_request", pull_request) == base

    push = _write_event(
        repo,
        {
            "ref": "refs/heads/main",
            "before": base,
            "after": head,
            "created": False,
            "deleted": False,
        },
    )
    assert resolve_github_base(repo, "push", push) == base


def test_resolve_github_base_uses_parent_for_all_zero_new_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    base = _commit(repo)
    module.write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "head")
    event = _write_event(
        repo,
        {
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )

    assert resolve_github_base(repo, "push", event) == base


def test_release_tag_uses_prior_reachable_semver_tag_instead_of_parent(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    previous_release = _commit(repo)
    _git(repo, "tag", "v0.2.5", previous_release)
    module.write_text("value = 2\n", encoding="utf-8")
    _commit(repo, "middle")
    module.write_text("value = 3\n", encoding="utf-8")
    head = _commit(repo, "release")
    _git(repo, "tag", "v0.3.0", head)
    event = _write_event(
        repo,
        {
            "ref": "refs/tags/v0.3.0",
            "before": "0" * 40,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )

    assert resolve_github_base(repo, "push", event) == previous_release


def test_first_release_tag_compares_every_source_file_with_empty_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\nother = 2\n", encoding="utf-8")
    head = _commit(repo)
    _git(repo, "tag", "v0.2.5", head)
    event = _write_event(
        repo,
        {
            "ref": "refs/tags/v0.2.5",
            "before": "0" * 40,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )

    base = resolve_github_base(repo, "push", event)
    changes = collect_changed_python(repo, base)

    assert changes.files == (
        ChangedFile(
            path=Path("src/reticulumpi/app.py"),
            lines=frozenset({1, 2}),
            status="A",
        ),
    )
    assert changes.base_commit == _git(repo, "hash-object", "-t", "tree", "/dev/null")


def test_v032_bootstrap_uses_pinned_version_boundary_over_lower_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, baseline, head, event = _bootstrap_release_repo(tmp_path)
    _git(repo, "tag", "v0.3.1", head)
    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.2": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.4",
                commit=baseline,
            )
        },
    )

    assert resolve_github_base(repo, "push", event) == baseline


def test_v033_bootstrap_ignores_withdrawn_v032_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, baseline, head, event = _bootstrap_release_repo(tmp_path, release_tag="v0.3.3")
    _git(repo, "tag", "v0.3.2", head)
    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.3": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.4",
                commit=baseline,
            )
        },
    )

    assert resolve_github_base(repo, "push", event) == baseline


def test_v033_bootstrap_and_current_default_are_locked() -> None:
    assert coverage_gate.DEFAULT_RELEASE_VERSION == "0.3.6"
    assert coverage_gate._RELEASE_BOOTSTRAP_BASELINES["v0.3.3"] == (
        coverage_gate.ReleaseBootstrapBaseline(
            historical_version="0.2.4",
            commit="89249b8b58cb86ac14ff7179abbbca3cb762d2a4",
        )
    )


def test_v032_bootstrap_fails_when_pinned_object_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _baseline, _head, event = _bootstrap_release_repo(tmp_path)
    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.2": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.4",
                commit="0" * 40,
            )
        },
    )

    with pytest.raises(CoverageGateError, match="does not resolve to a commit"):
        resolve_github_base(repo, "push", event)


def test_v032_bootstrap_requires_strict_ancestor_and_historical_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, baseline, head, event = _bootstrap_release_repo(tmp_path)
    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.2": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.4",
                commit=head,
            )
        },
    )
    with pytest.raises(CoverageGateError, match="not a strict first-parent ancestor"):
        resolve_github_base(repo, "push", event)

    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.2": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.3",
                commit=baseline,
            )
        },
    )
    with pytest.raises(CoverageGateError, match="expected '0.2.3'"):
        resolve_github_base(repo, "push", event)


def test_v032_bootstrap_rejects_second_parent_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    root = _commit(repo, "root")
    main_branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-q", "-b", "historical-boundary")
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "reticulumpi"\nversion = "0.2.4"\n',
        encoding="utf-8",
    )
    second_parent = _commit(repo, "historical version boundary")
    _git(repo, "checkout", "-q", main_branch)
    module.write_text("value = 2\n", encoding="utf-8")
    _commit(repo, "main change")
    _git(repo, "merge", "--no-ff", "--no-edit", "historical-boundary")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "tag", "v0.3.2", head)
    event = _write_event(
        repo,
        {
            "ref": "refs/tags/v0.3.2",
            "before": root,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )
    monkeypatch.setattr(
        coverage_gate,
        "_RELEASE_BOOTSTRAP_BASELINES",
        {
            "v0.3.2": coverage_gate.ReleaseBootstrapBaseline(
                historical_version="0.2.4",
                commit=second_parent,
            )
        },
    )

    with pytest.raises(CoverageGateError, match="not a strict first-parent ancestor"):
        resolve_github_base(repo, "push", event)


def test_post_bootstrap_release_uses_reachable_prior_tag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    previous = _commit(repo, "v0.3.3")
    _git(repo, "tag", "v0.3.3", previous)
    module.write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "v0.3.4")
    _git(repo, "tag", "v0.3.4", head)
    event = _write_event(
        repo,
        {
            "ref": "refs/tags/v0.3.4",
            "before": previous,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )

    assert resolve_github_base(repo, "push", event) == previous


def test_resolve_github_base_fails_closed_for_root_initial_push(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src/reticulumpi/app.py").write_text("value = 1\n", encoding="utf-8")
    head = _commit(repo)
    event = _write_event(
        repo,
        {
            "ref": "refs/heads/main",
            "before": "0" * 40,
            "after": head,
            "created": True,
            "deleted": False,
        },
    )

    with pytest.raises(CoverageGateError, match="initial push has no parent commit"):
        resolve_github_base(repo, "push", event)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"after": "0" * 40}, "all-zero destination"),
        ({"after": "HEAD_MISMATCH"}, "base revision does not resolve"),
        ({"before": "0" * 40, "created": False}, "all-zero base"),
        ({"deleted": True}, "deleted ref"),
        ({"ref": "refs/notes/test"}, "unsupported push ref"),
    ],
)
def test_resolve_github_base_rejects_inconsistent_push_payloads(
    tmp_path: Path, mutation: dict[str, object], message: str
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    module = repo / "src/reticulumpi/app.py"
    module.write_text("value = 1\n", encoding="utf-8")
    base = _commit(repo)
    module.write_text("value = 2\n", encoding="utf-8")
    head = _commit(repo, "head")
    payload: dict[str, object] = {
        "ref": "refs/heads/main",
        "before": base,
        "after": head,
        "created": False,
        "deleted": False,
    }
    if mutation.get("after") == "HEAD_MISMATCH":
        mutation = {**mutation, "after": base}
        message = "does not match checked-out HEAD"
    payload.update(mutation)
    event = _write_event(repo, payload)

    with pytest.raises(CoverageGateError, match=message):
        resolve_github_base(repo, "push", event)


def test_resolve_github_base_rejects_bad_event_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src/reticulumpi/app.py").write_text("value = 1\n", encoding="utf-8")
    _commit(repo)

    malformed = repo / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(CoverageGateError, match="cannot read GitHub event payload"):
        resolve_github_base(repo, "push", malformed)

    scalar = _write_event(repo, [])
    with pytest.raises(CoverageGateError, match="must be a JSON object"):
        resolve_github_base(repo, "push", scalar)

    missing = _write_event(repo, {})
    with pytest.raises(CoverageGateError, match="missing required field"):
        resolve_github_base(repo, "pull_request", missing)

    with pytest.raises(CoverageGateError, match="unsupported GitHub event"):
        resolve_github_base(repo, "schedule", missing)


def test_name_status_parser_rejects_truncated_and_unresolved_records(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src/reticulumpi").mkdir(parents=True)
    path = b"src/reticulumpi/conflict.py"
    (repo / path.decode()).write_text("value = 1\n", encoding="utf-8")

    with pytest.raises(CoverageGateError, match="truncated Git record"):
        _name_status_records(b"R100\0old.py\0", repo)
    with pytest.raises(CoverageGateError, match="unresolved Git status"):
        _name_status_records(b"U\0" + path + b"\0", repo)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"M\0path.py", "unterminated"),
        (b"\xff\0path.py\0", "invalid Git change status"),
        (b"\0path.py\0", "empty Git change status"),
        (b"Z\0path.py\0", "unsupported Git change status"),
        (b"R101\0old.py\0new.py\0", "invalid Git similarity status"),
        (b"M1\0path.py\0", "invalid Git change status"),
    ],
)
def test_name_status_parser_rejects_malformed_status_output(
    tmp_path: Path, payload: bytes, message: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(CoverageGateError, match=message):
        _name_status_records(payload, repo)


def test_git_path_rejects_non_utf8_and_missing_files(tmp_path: Path) -> None:
    with pytest.raises(CoverageGateError, match="not valid UTF-8"):
        _git_path(b"\xff.py", tmp_path, must_exist=False)
    with pytest.raises(CoverageGateError, match="not a regular file"):
        _git_path(b"missing.py", tmp_path, must_exist=True)
    target = tmp_path / "target.py"
    target.write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "alias.py").symlink_to(target)
    with pytest.raises(CoverageGateError, match="resolves through a symlink"):
        _git_path(b"alias.py", tmp_path, must_exist=True)


def test_changed_line_parser_handles_zero_ranges_and_binary_patches() -> None:
    path = Path("src/reticulumpi/module.py")
    assert _changed_lines_from_patch(b"@@ -1 +1,0 @@\n-old\n", path) == frozenset()
    with pytest.raises(CoverageGateError, match="binary file"):
        _changed_lines_from_patch(b"Binary files a/module.py and b/module.py differ\n", path)


def test_git_runner_passes_argument_vector_without_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stdout = b"safe"
        stderr = b""

    def fake_run(arguments: list[str], **kwargs: object) -> Result:
        captured["arguments"] = arguments
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setattr(subprocess, "run", fake_run)

    output = _run_git(tmp_path, "diff", "--", "$(touch danger)")

    assert output == b"safe"
    assert captured["arguments"] == ["git", "diff", "--", "$(touch danger)"]
    assert "shell" not in captured["kwargs"]  # subprocess defaults to shell=False.


def test_git_runner_reports_execution_and_command_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise OSError("git unavailable")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(CoverageGateError, match="could not execute Git"):
        _run_git(tmp_path, "status")

    class Failed:
        returncode = 2
        stdout = b""
        stderr = b"bad revision"

    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: Failed())
    with pytest.raises(CoverageGateError, match="bad revision"):
        _run_git(tmp_path, "diff", "bad")


def test_base_commit_rejects_invalid_git_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(coverage_gate, "_run_git", lambda *_args: b"\xff")
    with pytest.raises(CoverageGateError, match="non-ASCII"):
        _base_commit(tmp_path, "HEAD")

    monkeypatch.setattr(coverage_gate, "_run_git", lambda *_args: b"not-an-object\n")
    with pytest.raises(CoverageGateError, match="did not resolve to a commit"):
        _base_commit(tmp_path, "HEAD")


def test_cli_help_describes_fixed_policy(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--help"])

    assert raised.value.code == 0
    output = capsys.readouterr().out
    normalized = " ".join(output.split())
    assert "release-specific aggregate coverage" in normalized
    assert "90% coverage of changed executable ReticulumPi lines" in normalized
    assert "65% line/55% branch" in normalized
    assert "70% line/60% branch" in normalized
    assert "--coverage-xml" in output
    assert "--repo-root" in output
    assert "--release-version" in output
    assert "--github-event" in output


def test_cli_returns_success_policy_failure_and_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    records: dict[str, list[tuple[int, int, str | None]]] = {}
    for module in CRITICAL_MODULES:
        target = repo / module
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8")
        records[module.as_posix()] = [(1, 1, None)]
    base = _commit(repo)
    identity = repo / CRITICAL_MODULES[0]
    identity.write_text("value = 1\n# comment-only change\n", encoding="utf-8")
    xml = _write_coverage_xml(repo, records)

    assert main([base, "--repo-root", str(repo), "--coverage-xml", str(xml)]) == 0
    success = capsys.readouterr()
    assert "no executable production lines changed [PASS]" in success.out
    assert "Coverage gate passed." in success.out
    assert success.err == ""

    records[CRITICAL_MODULES[0].as_posix()] = [(1, 0, None)]
    xml = _write_coverage_xml(repo, records)
    assert main([base, "--repo-root", str(repo), "--coverage-xml", str(xml)]) == 1
    failure = capsys.readouterr()
    assert "FAIL src/reticulumpi/identity_manager.py" in failure.out
    assert "Coverage gate failed." in failure.out

    xml.write_text("<coverage>", encoding="utf-8")
    assert main([base, "--repo-root", str(repo), "--coverage-xml", str(xml)]) == 2
    invalid = capsys.readouterr()
    assert invalid.out == ""
    assert "Coverage gate input error: malformed coverage XML" in invalid.err


def test_cli_derives_github_base_and_applies_release_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    records: dict[str, list[tuple[int, int, str | None]]] = {}
    for module in CRITICAL_MODULES:
        target = repo / module
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8")
        records[module.as_posix()] = [(1, 1, None)]
    base = _commit(repo)
    _git(repo, "tag", "v0.3.0", base)
    identity = repo / CRITICAL_MODULES[0]
    identity.write_text("value = 1\n# release comment\n", encoding="utf-8")
    head = _commit(repo, "head")
    _git(repo, "tag", "v0.3.1", head)
    xml = _write_coverage_xml(repo, records)
    event = _write_event(
        repo,
        {
            "ref": "refs/tags/v0.3.1",
            "before": base,
            "after": head,
            "created": False,
            "deleted": False,
        },
    )

    status = main(
        [
            "--repo-root",
            str(repo),
            "--coverage-xml",
            str(xml),
            "--release-version",
            "v0.3.1",
            "--github-event",
            str(event),
            "--github-event-name",
            "push",
        ]
    )

    assert status == 0
    output = capsys.readouterr().out
    assert f"Changed-code base: {base}" in output
    assert "Aggregate coverage (0.3.1 policy):" in output
    assert "minimum 70%" in output
    assert "minimum 60%" in output


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["HEAD", "--github-event-name", "push"],
        ["--github-event", "event.json"],
        ["HEAD", "--github-event", "event.json", "--github-event-name", "push"],
        ["HEAD", "--release-version", "not-a-version"],
    ],
)
def test_cli_rejects_incomplete_or_conflicting_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    assert main([*arguments, "--repo-root", str(repo)]) == 2
    assert "Coverage gate input error:" in capsys.readouterr().err


def test_ci_runs_fail_closed_gate_after_serial_coverage() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["coverage"]["steps"]

    assert steps[0]["with"]["fetch-depth"] == 0
    coverage_index = next(
        index for index, step in enumerate(steps) if "--cov-report=xml" in step.get("run", "")
    )
    coverage_command = steps[coverage_index]["run"]
    assert "--cov=src/reticulumpi" in coverage_command
    assert "--cov=reticulumpi " not in coverage_command
    gate_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Enforce aggregate, changed-code, and critical-module coverage"
    )
    assert gate_index == coverage_index + 1
    command = steps[gate_index]["run"]
    assert "--release-version" in command
    assert "--github-event" in command
    assert "--github-event-name" in command
    assert steps[gate_index]["env"]["COVERAGE_RELEASE_VERSION"].endswith("|| '0.3.6' }}")
