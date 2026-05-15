"""API route handlers for signal operations."""

from __future__ import annotations

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import _error, _get_plugin, _ok


def _int_param(request: aiohttp.web.Request, name: str, default: int, max_val: int = 0) -> int:
    try:
        val = int(request.query.get(name, str(default)))
    except (ValueError, TypeError):
        val = default
    return min(val, max_val) if max_val else val


def _float_param(request: aiohttp.web.Request, name: str, default: float) -> float:
    try:
        return float(request.query.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _get_sigops(request: aiohttp.web.Request):
    plugin = _get_plugin(request)
    sigops = plugin.app.plugins.get("signal_operations")
    if sigops is None:
        return None
    return sigops


def _require_sigops(request: aiohttp.web.Request):
    sigops = _get_sigops(request)
    if sigops is None:
        return None, _error("signal_operations plugin not enabled", 404)
    return sigops, None


def _require_auth(request: aiohttp.web.Request) -> aiohttp.web.Response | None:
    if not request.get("token"):
        return _error("Authentication required", 401)
    return None


# ── GET /api/sigops ──────────────────────────────────────────────────


async def handle_sigops_overview(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    return _ok(sigops.get_overview())


# ── GET /api/sigops/contacts ─────────────────────────────────────────


async def handle_sigops_contacts(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    contact_type = request.query.get("type", "")
    limit = _int_param(request, "limit", 100, 500)
    return _ok(sigops.get_contacts(contact_type=contact_type, limit=limit))


# ── GET /api/sigops/contacts/{id} ────────────────────────────────────


async def handle_sigops_contact_detail(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    contact_id = request.match_info["id"]
    detail = sigops.get_contact_detail(contact_id)
    if detail is None:
        return _error("Contact not found", 404)
    return _ok(detail)


# ── GET /api/sigops/detections ───────────────────────────────────────


async def handle_sigops_detections(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    since = _float_param(request, "since", 0.0)
    freq_min = _int_param(request, "freq_min", 0)
    freq_max = _int_param(request, "freq_max", 0)
    signal_type = request.query.get("type", "")
    limit = _int_param(request, "limit", 100, 500)
    return _ok(sigops.get_detections(
        since=since, freq_min=freq_min, freq_max=freq_max,
        signal_type=signal_type, limit=limit,
    ))


# ── GET /api/sigops/baseline ────────────────────────────────────────


async def handle_sigops_baseline(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    return _ok(sigops.get_baseline())


# ── GET /api/sigops/correlations ─────────────────────────────────────


async def handle_sigops_correlations(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    limit = _int_param(request, "limit", 50, 200)
    return _ok(sigops.get_correlations(limit=limit))


# ── GET /api/sigops/ism ──────────────────────────────────────────────


async def handle_sigops_ism(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    plugin = _get_plugin(request)
    ism = plugin.app.plugins.get("ism_decoder")
    if ism is None:
        return _error("ism_decoder plugin not enabled", 404)
    return _ok(ism.get_device_inventory())


# ── GET /api/sigops/stats ────────────────────────────────────────────


async def handle_sigops_stats(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    sigops, err = _require_sigops(request)
    if err:
        return err
    return _ok(sigops.get_aggregate_stats())


# ── POST /api/sigops/classify ────────────────────────────────────────


async def handle_sigops_classify(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    sigops, err = _require_sigops(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)
    freq_hz = body.get("freq_hz")
    name = body.get("name")
    if not freq_hz or not name:
        return _error("freq_hz and name are required", 400)
    result = sigops.manual_classify(
        freq_hz=int(freq_hz), name=str(name),
        extra=body.get("extra"),
    )
    return _ok(result)


# ── POST /api/sigops/baseline/reset ──────────────────────────────────


async def handle_sigops_baseline_reset(
    request: aiohttp.web.Request,
) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    sigops, err = _require_sigops(request)
    if err:
        return err
    sigops.reset_baseline()
    return _ok({"status": "baseline_reset"})


# ── route registration ───────────────────────────────────────────────


def setup_sigops_routes(app: aiohttp.web.Application) -> None:
    app.router.add_get("/api/sigops", handle_sigops_overview)
    app.router.add_get("/api/sigops/contacts", handle_sigops_contacts)
    app.router.add_get("/api/sigops/contacts/{id}", handle_sigops_contact_detail)
    app.router.add_get("/api/sigops/detections", handle_sigops_detections)
    app.router.add_get("/api/sigops/baseline", handle_sigops_baseline)
    app.router.add_get("/api/sigops/correlations", handle_sigops_correlations)
    app.router.add_get("/api/sigops/ism", handle_sigops_ism)
    app.router.add_get("/api/sigops/stats", handle_sigops_stats)
    app.router.add_post("/api/sigops/classify", handle_sigops_classify)
    app.router.add_post("/api/sigops/baseline/reset", handle_sigops_baseline_reset)
