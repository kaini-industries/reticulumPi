"""Regression tests for deterministic documentation CI."""

from tools import check_docs
from tools import generate_docs_reference
from tools.run_doc_shell_examples import TAG, discover_examples


def test_repository_documentation_gate_passes():
    assert check_docs.run_checks() == []


def test_bookworm_shell_examples_are_explicitly_tagged_and_discoverable():
    examples = discover_examples()

    assert examples
    assert all(example.script.strip() for example in examples)
    assert all(TAG in example.path.read_text(encoding="utf-8") for example in examples)


def test_stale_reference_rules_reject_normative_legacy_claims(tmp_path):
    document = tmp_path / "guide.md"
    text = """Use /opt/reticulumpi/.venv/bin/python.
allow_localhost_api: true
Read /etc/reticulumpi/dashboard_password.txt.
The generated password is logged once.
Supported on Python 3.10.
There are 99 plugins.
"""
    errors = check_docs.stale_reference_errors(document, text)
    assert len(errors) == 6


def test_stale_reference_rules_reject_unsupported_production_paths(tmp_path):
    document = tmp_path / "guide.md"
    text = """Run /opt/reticulumpi/scripts/nomadnet-tui.sh.
Store MeshChat in /opt/reticulumpi/meshchat.
Install with --with-meshchat.
"""
    errors = check_docs.stale_reference_errors(document, text)
    assert len(errors) == 3


def test_stale_reference_rules_allow_only_labeled_legacy_service_home(tmp_path):
    document = tmp_path / "guide.md"
    stale = "Write state under /home/reticulumpi/.local/share.\n"
    legacy = "/home/reticulumpi is a legacy migration input only.\n"
    assert len(check_docs.stale_reference_errors(document, stale)) == 1
    assert check_docs.stale_reference_errors(document, legacy) == []


def test_canonical_password_and_current_release_paths_are_allowed(tmp_path):
    document = tmp_path / "guide.md"
    text = """Run /opt/reticulumpi/current/.venv/bin/reticulumpi.
Read /var/lib/reticulumpi/.config/reticulumpi/dashboard_password.txt as root.
There is no anonymous localhost bypass.
allowed_identities: []  # Empty = accept from anyone
"""
    errors = check_docs.stale_reference_errors(document, text)
    assert len(errors) == 1
    assert "empty-allowlist" in errors[0]


def test_local_link_check_reports_missing_target(tmp_path):
    document = tmp_path / "guide.md"
    document.write_text("See [missing](not-there.md).\n", encoding="utf-8")
    errors = check_docs.check_local_links([document])
    assert len(errors) == 1
    assert "not-there.md" in errors[0]


def test_historical_documents_are_excluded_from_normative_scan():
    normative = {path.relative_to(check_docs.ROOT) for path in check_docs.normative_text_files()}
    assert not (normative & check_docs.HISTORICAL_DOCUMENTS)


def test_generated_reference_matches_route_default_plugin_and_event_sources():
    assert generate_docs_reference.reference_diff() is None
    rendered = generate_docs_reference.render_reference()
    assert f"{len(generate_docs_reference.builtin_plugins())} plugin classes" in rendered
    assert f"{len(generate_docs_reference.event_constants())} unique public event names" in rendered
    assert (
        f"{len(generate_docs_reference.dashboard_routes())} unique HTTP/WebSocket registrations"
        in rendered
    )
    assert "## Core configuration defaults" in rendered


def test_generated_reference_detects_stale_snapshot(tmp_path):
    stale = tmp_path / "reference.md"
    stale.write_text("stale\n", encoding="utf-8")
    difference = generate_docs_reference.reference_diff(stale)
    assert difference is not None
    assert "current source-derived reference" in difference


def test_audit_ledger_requires_exact_52_id_contract(tmp_path):
    ledger = tmp_path / "ledger.md"
    ledger.write_text("| P1-01 | only row |\n| P1-01 | duplicate |\n", encoding="utf-8")
    errors = check_docs.audit_ledger_errors(ledger)
    assert any("expected exactly 52" in error for error in errors)
    assert any("duplicate audit IDs" in error for error in errors)
    assert any("missing audit IDs" in error for error in errors)
