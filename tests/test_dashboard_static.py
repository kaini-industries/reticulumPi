"""Static dashboard accessibility and offline-shell regression contracts."""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Self

import pytest


STATIC = (
    Path(__file__).parents[1]
    / "src"
    / "reticulumpi"
    / "builtin_plugins"
    / "web_dashboard"
    / "static"
)


@dataclass
class _Element:
    tag: str
    attrs: dict[str, str | None]
    line: int
    parent: Self | None = None
    children: list[Self] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    def descendants(self, tag: str | None = None) -> list[Self]:
        matches: list[_Element] = []
        for child in self.children:
            if tag is None or child.tag == tag:
                matches.append(child)
            matches.extend(child.descendants(tag))
        return matches

    def closest(self, tag: str) -> Self | None:
        current = self.parent
        while current is not None:
            if current.tag == tag:
                return current
            current = current.parent
        return None


class _StrictHTMLParser(HTMLParser):
    """Build a small DOM while rejecting mismatched or unclosed source markup."""

    VOID_ELEMENTS = frozenset(
        {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = _Element("#document", {}, 0)
        self._stack = [self.document]
        self.errors: list[str] = []

    def _add_element(self, tag: str, attrs: list[tuple[str, str | None]], *, closed: bool) -> None:
        names = [name for name, _ in attrs]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            self.errors.append(
                f"line {self.getpos()[0]}: duplicate attributes on <{tag}>: {duplicates}"
            )
        element = _Element(
            tag=tag,
            attrs=dict(attrs),
            line=self.getpos()[0],
            parent=self._stack[-1],
        )
        self._stack[-1].children.append(element)
        if not closed and tag not in self.VOID_ELEMENTS:
            self._stack.append(element)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add_element(tag, attrs, closed=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add_element(tag, attrs, closed=True)

    def handle_endtag(self, tag: str) -> None:
        line = self.getpos()[0]
        if tag in self.VOID_ELEMENTS:
            self.errors.append(f"line {line}: void element </{tag}> must not be closed")
            return
        if len(self._stack) == 1:
            self.errors.append(f"line {line}: unexpected closing tag </{tag}>")
            return
        current = self._stack[-1]
        if current.tag != tag:
            self.errors.append(
                f"line {line}: closing </{tag}> while <{current.tag}> "
                f"from line {current.line} is still open"
            )
            return
        self._stack.pop()

    def handle_data(self, data: str) -> None:
        self._stack[-1].text.append(data)

    def finish(self) -> None:
        self.close()
        for element in self._stack[1:]:
            self.errors.append(f"line {element.line}: unclosed <{element.tag}> element")


def _parse_page(filename: str) -> _StrictHTMLParser:
    parser = _StrictHTMLParser()
    parser.feed((STATIC / filename).read_text())
    parser.finish()
    return parser


def _parse_dashboard() -> _StrictHTMLParser:
    return _parse_page("index.html")


def _class_names(element: _Element) -> set[str]:
    return set((element.attrs.get("class") or "").split())


def _text_content(element: _Element) -> str:
    return " ".join(element.text + [_text_content(child) for child in element.children])


def test_login_password_has_programmatic_label_and_live_error():
    html = (STATIC / "login.html").read_text()
    assert '<label class="sr-only" for="password">' in html
    assert 'id="error" class="error" role="alert"' in html


def test_destructive_actions_use_native_accessible_dialogs():
    html = (STATIC / "index.html").read_text()
    assert '<dialog class="confirm-dialog" id="restart-dialog"' in html
    assert '<dialog class="confirm-dialog" id="destructive-dialog"' in html
    assert 'aria-describedby="destructive-dialog-message"' in html
    assert 'method="dialog"' in html
    assert '<dialog class="dialog-overlay" id="channel-dialog-overlay"' in html
    assert 'aria-labelledby="restart-dialog-title"' in html
    assert 'aria-label="Close channel manager"' in html


def test_dashboard_markup_is_balanced_and_sections_are_named_top_level_regions():
    parser = _parse_dashboard()
    sections = parser.document.descendants("section")

    assert parser.errors == []
    assert len(sections) == 34
    assert all(section.closest("section") is None for section in sections)
    assert all(
        (section.attrs.get("aria-label") or section.attrs.get("aria-labelledby"))
        for section in sections
    )


def test_dashboard_ids_and_collapsible_control_targets_are_unique_and_truthful():
    parser = _parse_dashboard()
    source = (STATIC / "app.js").read_text()
    elements = parser.document.descendants()
    elements_with_ids = [element for element in elements if element.attrs.get("id")]
    ids = [element.attrs["id"] for element in elements_with_ids]
    by_id = {element.attrs["id"]: element for element in elements_with_ids}
    controls = [
        element
        for element in elements
        if "collapsible" in _class_names(element) and "section-header" in _class_names(element)
    ]

    assert len(ids) == len(set(ids))
    assert len(controls) == 23
    assert all(control.tag == "button" for control in controls)
    assert all(control.attrs.get("type") == "button" for control in controls)

    target_ids: list[str] = []
    for control in controls:
        target_id = control.attrs.get("aria-controls")
        expanded = control.attrs.get("aria-expanded")
        assert target_id is not None
        assert target_id in by_id
        assert expanded in {"true", "false"}
        target_ids.append(target_id)

        target = by_id[target_id]
        target_classes = _class_names(target)
        control_classes = _class_names(control)
        class_hidden = "hidden" in target_classes
        native_hidden = "hidden" in target.attrs

        assert target.closest("section") is control.closest("section")
        assert target.tag == "div"
        assert class_hidden is native_hidden
        assert (expanded == "false") is class_hidden
        assert (expanded == "true") is ("open" in control_classes)

    assert len(target_ids) == len(set(target_ids))
    assert sum(control.attrs["aria-expanded"] == "true" for control in controls) == 1
    assert "control.getAttribute('aria-controls')" in source
    assert "if (control.tagName !== 'BUTTON')" in source


def test_dashboard_tables_have_unique_captions_and_scoped_column_headers():
    parser = _parse_dashboard()
    tables = parser.document.descendants("table")
    caption_names: list[str] = []

    assert len(tables) == 23
    for table in tables:
        captions = [child for child in table.children if child.tag == "caption"]
        headers = table.descendants("th")

        assert len(captions) == 1
        assert table.children[0] is captions[0]
        assert _text_content(captions[0]).strip()
        caption_names.append(_text_content(captions[0]).strip())
        assert headers
        assert all(header.attrs.get("scope") == "col" for header in headers)

    assert len(caption_names) == len(set(caption_names))


def test_dashboard_lazy_controls_have_source_level_accessible_names():
    parser = _parse_dashboard()
    elements = parser.document.descendants()
    by_id = {element.attrs["id"]: element for element in elements if element.attrs.get("id")}
    labels_by_target = {
        label.attrs["for"]: label
        for label in parser.document.descendants("label")
        if label.attrs.get("for")
    }

    for field_id in (
        "msg-lora-channel-select",
        "radio-freq-input",
        "radio-gain",
        "radio-squelch",
        "radio-volume",
        "rt-search",
        "rt-iface-filter",
        "rt-hops-filter",
    ):
        assert field_id in labels_by_target
        assert _text_content(labels_by_target[field_id]).strip()

    bridge_toggle = by_id["mesh-bridge-toggle"]
    label_ids = (bridge_toggle.attrs.get("aria-labelledby") or "").split()
    assert label_ids == ["mesh-bridge-toggle-label"]
    assert all(label_id in by_id for label_id in label_ids)
    assert _text_content(by_id[label_ids[0]]).strip() == "Relaying"


def test_dashboard_source_visualizations_have_accessible_names():
    parser = _parse_dashboard()
    elements = parser.document.descendants()
    by_id = {element.attrs["id"]: element for element in elements if element.attrs.get("id")}

    for visual in parser.document.descendants("svg") + parser.document.descendants("canvas"):
        assert visual.attrs.get("role") == "img", f"unnamed visual at line {visual.line}"
        assert visual.attrs.get("aria-label"), f"unnamed visual at line {visual.line}"

    for chart_id in ("link-tester-rtt-chart", "link-tester-signal-chart"):
        described_by = set((by_id[chart_id].attrs.get("aria-describedby") or "").split())
        assert described_by == {"link-tester-summary", "link-tester-log-caption"}
        assert all(reference in by_id for reference in described_by)


def test_spectrum_page_has_landmarks_named_sections_and_semantic_disclosures():
    parser = _parse_page("spectrum.html")
    elements = parser.document.descendants()
    sections = parser.document.descendants("section")
    headers = parser.document.descendants("header")
    mains = parser.document.descendants("main")
    elements_with_ids = [element for element in elements if element.attrs.get("id")]
    ids = [element.attrs["id"] for element in elements_with_ids]
    by_id = {element.attrs["id"]: element for element in elements_with_ids}
    controls = [
        element
        for element in elements
        if "section-header" in _class_names(element) and "collapsible" in _class_names(element)
    ]

    assert parser.errors == []
    assert len(headers) == 1
    assert len(mains) == 1
    assert mains[0].attrs.get("id") == "spectrum-main"
    assert len(sections) == 3
    assert all(section.closest("section") is None for section in sections)
    assert all(section.attrs.get("aria-label") for section in sections)
    assert len(ids) == len(set(ids))
    assert len(controls) == 3

    target_ids: list[str] = []
    for control in controls:
        target_id = control.attrs.get("aria-controls")
        assert target_id is not None
        target_ids.append(target_id)
        target = by_id[target_id]

        assert control.tag == "button"
        assert control.attrs.get("type") == "button"
        assert control.attrs.get("aria-expanded") == "false"
        assert "open" not in _class_names(control)
        assert target.closest("section") is control.closest("section")
        assert "hidden" in _class_names(target)
        assert "hidden" in target.attrs

    assert len(target_ids) == len(set(target_ids))


def test_spectrum_visuals_and_link_results_have_text_alternatives():
    parser = _parse_page("spectrum.html")
    elements = parser.document.descendants()
    by_id = {element.attrs["id"]: element for element in elements if element.attrs.get("id")}
    table = parser.document.descendants("table")[0]
    captions = [child for child in table.children if child.tag == "caption"]

    for visual in parser.document.descendants("svg") + parser.document.descendants("canvas"):
        assert visual.attrs.get("role") == "img"
        assert visual.attrs.get("aria-label")

    for plot_id, help_id in (
        ("spectrum-line", "spectrum-keyboard-help"),
        ("lora-spectrum-line", "lora-spectrum-keyboard-help"),
    ):
        plot = by_id[plot_id]
        assert plot.attrs.get("tabindex") == "0"
        assert plot.attrs.get("aria-describedby") == help_id
        assert _text_content(by_id[help_id]).strip()

    for chart_id in ("link-tester-rtt-chart", "link-tester-signal-chart"):
        described_by = set((by_id[chart_id].attrs.get("aria-describedby") or "").split())
        assert described_by == {"link-tester-summary", "link-tester-log-caption"}
        assert all(reference in by_id for reference in described_by)

    assert len(captions) == 1
    assert table.children[0] is captions[0]
    assert _text_content(captions[0]).strip()
    assert all(header.attrs.get("scope") == "col" for header in table.descendants("th"))


def test_spectrum_secondary_disclosures_are_native_buttons():
    parser = _parse_page("spectrum.html")
    by_id = {
        element.attrs["id"]: element
        for element in parser.document.descendants()
        if element.attrs.get("id")
    }

    for control_id in (
        "spectrum-zoom-reset",
        "lora-spec-zoom-reset",
        "spectrum-legend-toggle",
    ):
        control = by_id[control_id]
        assert control.tag == "button"
        assert control.attrs.get("type") == "button"

    legend_control = by_id["spectrum-legend-toggle"]
    legend = by_id[legend_control.attrs["aria-controls"]]
    assert legend_control.attrs.get("aria-expanded") == "false"
    assert "hidden" in _class_names(legend)
    assert "hidden" in legend.attrs


@pytest.mark.parametrize("filename", ["spectrum.js", "lora_spectrum.js"])
def test_spectrum_interactions_use_pointer_keyboard_and_hidpi_canvas(filename):
    source = (STATIC / filename).read_text()

    assert "window.devicePixelRatio" in source
    assert "WF_HISTORY_ROWS" in source
    assert "addEventListener('pointerdown', _onDragStart)" in source
    assert "addEventListener('pointermove', _onHover)" in source
    assert "addEventListener('keydown', _onZoomKeyDown)" in source
    assert "addEventListener('pointercancel', _onDragCancel)" in source
    assert "setPointerCapture" in source
    assert "releasePointerCapture" in source
    assert "addEventListener('mousedown'" not in source
    assert "addEventListener('mousemove'" not in source
    assert "addEventListener('mouseup'" not in source
    for key in ("ArrowLeft", "ArrowRight", "Enter", "Escape", "Home"):
        assert key in source

    common = (STATIC / "spectrum_common.js").read_text()
    assert "historyCapacity" in common
    assert "rows / capacity" in common
    assert "maxRows / capacity" in common

    css = (STATIC / "style.css").read_text()
    assert "touch-action: none" in css


def test_destructive_paths_do_not_use_blocking_browser_confirm():
    sources = "\n".join(
        path.read_text() for path in STATIC.glob("*.js") if path.name != "version.js"
    )
    app_source = (STATIC / "app.js").read_text()

    assert "window.confirm(" not in sources
    assert "if (!confirm(" not in sources
    assert "RPI.confirmDestructive = confirmDestructive" in app_source


def test_dashboard_exposes_skip_link_and_live_operational_state():
    html = (STATIC / "index.html").read_text()
    assert 'class="skip-link" href="#main-content"' in html
    assert 'id="main-content" tabindex="-1"' in html
    banner = re.search(r'<div\b[^>]*\bid="firmware-hang-banner"[^>]*>', html)
    assert banner is not None
    class_attr = re.search(r'\bclass="([^"]*)"', banner.group(0))
    assert class_attr is not None
    assert "firmware-hang-banner" in class_attr.group(1).split()
    assert 'role="alert"' in banner.group(0)
    assert 'aria-live="assertive"' in banner.group(0)


def test_csp_safe_initial_visibility_is_transferred_to_cssom_state():
    html = (STATIC / "index.html").read_text()
    css = (STATIC / "style.css").read_text()
    source = (STATIC / "app.js").read_text()

    assert "csp-initial-hidden" in html
    assert ".csp-initial-hidden { display: none; }" in css
    assert "document.querySelectorAll('.csp-initial-hidden')" in source
    assert "classList.remove('csp-initial-hidden')" in source


def test_styles_cover_reduced_motion_forced_colors_and_touch():
    css = (STATIC / "style.css").read_text()
    assert "@media (prefers-reduced-motion: reduce)" in css
    assert "@media (forced-colors: active)" in css
    assert "@media (pointer: coarse)" in css
    assert "min-height: 44px" in css


def test_service_worker_has_navigation_fallback_and_awaits_cache_work():
    source = (STATIC / "sw.js").read_text()
    assert "e.request.mode === 'navigate'" in source
    assert "_navigationFetch(e.request)" in source
    assert "cache.match(fallbackPath)" in source
    assert "var fallbackPath = _navigationFallbackPath(path)" in source
    assert "if (path === '/spectrum.html') return '/spectrum.html';" in source
    assert "event.waitUntil(networkFetch" in source
    assert "path === '/' || path === '/index.html' || path === '/spectrum.html'" in source


def test_service_worker_activates_only_after_the_complete_shell_is_cached():
    source = (STATIC / "sw.js").read_text()
    install = source.split("self.addEventListener('install'", 1)[1].split(
        "self.addEventListener('activate'", 1
    )[0]
    activate = source.split("self.addEventListener('activate'", 1)[1].split(
        "self.addEventListener('fetch'", 1
    )[0]

    assert install.index("cache.addAll(SHELL_ASSETS)") < install.index("self.skipWaiting()")
    assert "caches.delete" not in install
    assert "caches.delete" in activate


def test_shared_api_helper_owns_json_serialization():
    app_source = (STATIC / "app.js").read_text()
    login_source = (STATIC / "login.js").read_text()
    errlog_source = (STATIC / "errlog.js").read_text()
    transport_source = (STATIC / "json_fetch.js").read_text()
    link_source = (STATIC / "link_tester.js").read_text()

    assert "body: hasJson ? JSON.stringify(options.json)" in transport_source
    assert "JSON request callers must pass json, not body" in transport_source
    for source in (app_source, login_source, errlog_source, link_source):
        assert "body: JSON.stringify" not in source
        assert "json:" in source
    assert "RPI.jsonFetch(path" in app_source
    assert "window.RPI.jsonFetch('/api/auth/login'" in login_source
    assert '.jsonFetch("/api/client_error"' in errlog_source
