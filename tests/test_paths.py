"""Tests for installed distribution resource discovery."""

from __future__ import annotations

from pathlib import Path

from reticulumpi import _paths


class _DistributionEntry:
    def __init__(self, recorded: str, located: Path) -> None:
        self.recorded = recorded
        self.located = located

    def __str__(self) -> str:
        return self.recorded

    def locate(self) -> Path:
        return self.located


def test_distribution_asset_resolves_file(monkeypatch, tmp_path):
    page = tmp_path / "share" / "reticulumpi" / "nomadnet" / "pages" / "index.mu"
    page.parent.mkdir(parents=True)
    page.write_text("index", encoding="utf-8")
    entry = _DistributionEntry(
        "../../../share/reticulumpi/nomadnet/pages/index.mu",
        page,
    )
    monkeypatch.setattr(_paths, "files", lambda _name: [entry])

    assert _paths.find_distribution_asset("nomadnet", "pages", "index.mu") == str(page)


def test_distribution_asset_resolves_parent_directory(monkeypatch, tmp_path):
    page = tmp_path / "share" / "reticulumpi" / "nomadnet" / "pages" / "index.mu"
    page.parent.mkdir(parents=True)
    page.write_text("index", encoding="utf-8")
    entry = _DistributionEntry(
        "../../../share/reticulumpi/nomadnet/pages/index.mu",
        page,
    )
    monkeypatch.setattr(_paths, "files", lambda _name: [entry])

    assert _paths.find_distribution_asset("nomadnet", "pages") == str(page.parent)
