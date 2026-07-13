"""Static regression guard for first-party RNS lifecycle ownership."""

from __future__ import annotations

import ast
from pathlib import Path


_BUILTINS = Path(__file__).parents[1] / "src" / "reticulumpi" / "builtin_plugins"
_OWNER_CALLS = {"manage_destination", "_manage_lxmf_destination", "_own_destination"}
_EXPLICIT_DESTINATION_CLEANUP = {
    ("alert_system.py", "_get_recipient_destination"),
    ("transport_monitor.py", "_query_peer_hubs"),
}
_EXPLICIT_LINK_CLEANUP = {("transport_monitor.py", "_query_peer_hubs")}


def _call_name(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return None


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
    return "<module>"


def _has_owner_ancestor(parents: dict[ast.AST, ast.AST], node: ast.AST) -> bool:
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, ast.Call) and _call_name(current) in _OWNER_CALLS:
            return True
    return False


def _is_rns_constructor(node: ast.Call, name: str) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == name
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "RNS"
    )


def test_builtin_rns_resources_are_lifecycle_owned() -> None:
    unmanaged: list[str] = []

    for path in sorted(_BUILTINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)
        }
        function_calls: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = _enclosing_function(parents, node)
            call_name = _call_name(node)
            if call_name:
                function_calls.setdefault(function_name, set()).add(call_name)

            site = (path.name, function_name)
            if (
                _is_rns_constructor(node, "Destination")
                or call_name == "register_delivery_identity"
            ):
                if not _has_owner_ancestor(parents, node) and site not in (
                    _EXPLICIT_DESTINATION_CLEANUP
                ):
                    unmanaged.append(f"{path.name}:{node.lineno} destination")
            elif _is_rns_constructor(node, "Link") and site not in _EXPLICIT_LINK_CLEANUP:
                if not _has_owner_ancestor(parents, node):
                    unmanaged.append(f"{path.name}:{node.lineno} link")

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "register_request_handler":
                continue
            function_name = _enclosing_function(parents, node)
            if "manage_request_handler" not in function_calls.get(function_name, set()):
                unmanaged.append(f"{path.name}:{node.lineno} request handler")

    assert unmanaged == [], "unmanaged first-party RNS resources:\n" + "\n".join(unmanaged)
