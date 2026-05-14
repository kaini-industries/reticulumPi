"""API route handlers for the FM/AM radio receiver."""

from __future__ import annotations

import asyncio
import struct

import aiohttp.web

from reticulumpi.builtin_plugins.web_dashboard.api import _error, _get_plugin, _ok


def _get_fm_receiver(request: aiohttp.web.Request):
    plugin = _get_plugin(request)
    fm = plugin.app.plugins.get("fm_receiver")
    if not fm:
        return None
    return fm


def _require_fm(request: aiohttp.web.Request):
    fm = _get_fm_receiver(request)
    if fm is None:
        return None, _error("fm_receiver plugin not enabled", 404)
    return fm, None


def _require_auth(request: aiohttp.web.Request) -> aiohttp.web.Response | None:
    if not request.get("token"):
        return _error("Authentication required", 401)
    return None


def _build_wav_header(sample_rate: int, channels: int = 1, bits: int = 16) -> bytes:
    byte_rate = sample_rate * channels * (bits // 8)
    block_align = channels * (bits // 8)
    data_size = 0x7FFFFFFF
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits,
        b"data",
        data_size,
    )
    return header


# ── GET /api/radio/status ────────────────────────────────────────────


async def handle_radio_status(request: aiohttp.web.Request) -> aiohttp.web.Response:
    fm, err = _require_fm(request)
    if err:
        return err
    return _ok(fm.get_snapshot())


# ── POST /api/radio/tune ────────────────────────────────────────────


async def handle_radio_tune(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    freq_mhz = body.get("frequency_mhz")
    if freq_mhz is None:
        return _error("'frequency_mhz' field required", 400)
    try:
        freq_mhz = float(freq_mhz)
    except (TypeError, ValueError):
        return _error("'frequency_mhz' must be a number", 400)

    mode = body.get("mode")
    try:
        result = fm.tune(int(freq_mhz * 1_000_000), mode=mode)
    except ValueError as exc:
        return _error(str(exc), 400)
    return _ok(result)


# ── POST /api/radio/play ────────────────────────────────────────────


async def handle_radio_play(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    result = fm.play()
    return _ok(result)


# ── POST /api/radio/stop ────────────────────────────────────────────


async def handle_radio_stop(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    result = fm.stop_playback()
    return _ok(result)


# ── POST /api/radio/gain ────────────────────────────────────────────


async def handle_radio_gain(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    gain_db = body.get("gain_db")
    if gain_db is not None:
        try:
            gain_db = float(gain_db)
        except (TypeError, ValueError):
            return _error("'gain_db' must be a number or null", 400)
    try:
        result = fm.set_gain(gain_db)
    except ValueError as exc:
        return _error(str(exc), 400)
    return _ok(result)


# ── POST /api/radio/squelch ─────────────────────────────────────────


async def handle_radio_squelch(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    level = body.get("level")
    if level is None:
        return _error("'level' field required", 400)
    try:
        level = int(level)
    except (TypeError, ValueError):
        return _error("'level' must be an integer", 400)
    result = fm.set_squelch(level)
    return _ok(result)


# ── POST /api/radio/volume ──────────────────────────────────────────


async def handle_radio_volume(request: aiohttp.web.Request) -> aiohttp.web.Response:
    auth_err = _require_auth(request)
    if auth_err:
        return auth_err
    fm, err = _require_fm(request)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return _error("Invalid JSON body", 400)

    volume = body.get("volume")
    if volume is None:
        return _error("'volume' field required", 400)
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        return _error("'volume' must be a number", 400)
    if not 0.0 <= volume <= 1.0:
        return _error("'volume' must be between 0.0 and 1.0", 400)
    result = fm.set_volume(volume)
    return _ok(result)


# ── GET /api/radio/presets ───────────────────────────────────────────


async def handle_radio_presets(request: aiohttp.web.Request) -> aiohttp.web.Response:
    fm, err = _require_fm(request)
    if err:
        return err
    return _ok(fm.get_presets())


# ── GET /api/radio/audio ────────────────────────────────────────────


async def handle_radio_audio(request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    if not request.get("token"):
        raise aiohttp.web.HTTPUnauthorized(text="Authentication required")

    plugin = _get_plugin(request)
    fm = plugin.app.plugins.get("fm_receiver")
    if not fm:
        raise aiohttp.web.HTTPNotFound(text="fm_receiver plugin not enabled")
    if not fm._playing:
        raise aiohttp.web.HTTPConflict(text="Radio is not playing")

    fm.set_event_loop(asyncio.get_event_loop())

    response = aiohttp.web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "audio/wav",
            "Cache-Control": "no-cache, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
    response.enable_chunked_encoding()
    await response.prepare(request)

    wav_header = _build_wav_header(
        sample_rate=fm._output_rate_hz,
        channels=1,
        bits=16,
    )
    await response.write(wav_header)

    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    fm.register_audio_client(queue)
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=10.0)
            except asyncio.TimeoutError:
                if not fm._playing:
                    break
                continue
            if chunk is None:
                break
            await response.write(chunk)
    except (asyncio.CancelledError, ConnectionResetError, ConnectionAbortedError):
        pass
    finally:
        fm.unregister_audio_client(queue)

    return response


# ── Route registration ───────────────────────────────────────────────


def setup_radio_routes(app: aiohttp.web.Application) -> None:
    app.router.add_get("/api/radio/status", handle_radio_status)
    app.router.add_post("/api/radio/tune", handle_radio_tune)
    app.router.add_post("/api/radio/play", handle_radio_play)
    app.router.add_post("/api/radio/stop", handle_radio_stop)
    app.router.add_post("/api/radio/gain", handle_radio_gain)
    app.router.add_post("/api/radio/squelch", handle_radio_squelch)
    app.router.add_post("/api/radio/volume", handle_radio_volume)
    app.router.add_get("/api/radio/presets", handle_radio_presets)
    app.router.add_get("/api/radio/audio", handle_radio_audio)
