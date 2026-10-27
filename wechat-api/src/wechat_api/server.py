"""HTTP and WebSocket surface: /api/health, /api/onebot/<action>, /api/onebot/ws, plus
the reverse WebSocket client that connects to AstrBot."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import time
from typing import Any

import aiohttp
from aiohttp import web

from . import __version__
from .events import meta_event
from .gateway import Gateway, failed

log = logging.getLogger(__name__)

GATEWAY_KEY = web.AppKey("gateway", Gateway)


def token_from_request(request: web.Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() in {"bearer", "token"} and value.strip():
            return value.strip()
    return request.query.get("access_token")


def check_token(request: web.Request, expected: str) -> None:
    if not expected:
        raise web.HTTPServiceUnavailable(
            text=json.dumps(failed("WECHAT_API_TOKEN 未配置，OneBot 接口已关闭", 503)),
            content_type="application/json",
        )
    given = token_from_request(request)
    if given is None:
        raise web.HTTPUnauthorized(text=json.dumps(failed("缺少 access_token", 401)), content_type="application/json")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        raise web.HTTPForbidden(text=json.dumps(failed("access_token 不正确", 403)), content_type="application/json")


async def handle_health(request: web.Request) -> web.Response:
    gw = request.app[GATEWAY_KEY]
    return web.json_response(gw.state.health(), dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def handle_action(request: web.Request) -> web.Response:
    gw = request.app[GATEWAY_KEY]
    check_token(request, gw.cfg.access_token)
    params: dict[str, Any] = dict(request.query)
    params.pop("access_token", None)
    if request.method == "POST" and request.can_read_body:
        try:
            if request.content_type == "application/json":
                body = await request.json()
            else:
                body = dict(await request.post())
        except ValueError:
            return web.json_response(failed("请求体不是合法的 JSON", 100), status=400)
        if not isinstance(body, dict):
            return web.json_response(failed("请求体必须是 JSON 对象", 100), status=400)
        params.update(body)
    result = await gw.call(request.match_info["action"], params)
    status = 404 if result["retcode"] == 1404 else 200
    return web.json_response(result, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False))


async def serve_ws(gw: Gateway, ws: web.WebSocketResponse | aiohttp.ClientWebSocketResponse, label: str) -> None:
    """Shared loop for forward and reverse connections: push events, answer actions."""
    send_lock = asyncio.Lock()

    async def send(obj: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send_str(json.dumps(obj, ensure_ascii=False))

    gw.sinks.add(send)
    try:
        await send(meta_event("lifecycle", gw.self_id, sub_type="connect"))
        async for frame in ws:
            if frame.type != aiohttp.WSMsgType.TEXT:
                if frame.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
                continue
            try:
                req = json.loads(frame.data)
                if not isinstance(req, dict):
                    raise ValueError
            except ValueError:
                await send(failed("不是合法的 JSON 动作", 100))
                continue

            async def run(req: dict[str, Any]) -> None:
                result = await gw.call(str(req.get("action", "")), req.get("params") or {})
                if "echo" in req:
                    result["echo"] = req["echo"]
                await send(result)

            # Actions run concurrently so a slow send does not block get_* calls.
            asyncio.create_task(run(req))
    finally:
        gw.sinks.discard(send)
        log.info("%s websocket closed", label)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    gw = request.app[GATEWAY_KEY]
    check_token(request, gw.cfg.access_token)
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    gw.state.ws_clients += 1
    try:
        await serve_ws(gw, ws, "forward")
    finally:
        gw.state.ws_clients -= 1
    return ws


def build_app(gw: Gateway) -> web.Application:
    app = web.Application()
    app[GATEWAY_KEY] = gw
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/onebot/ws", handle_ws)
    app.router.add_route("*", "/api/onebot/{action}", handle_action)
    return app


def backoff_delay(attempt: int, max_delay: float, jitter: float = 0.2) -> float:
    base = min(max_delay, 2 ** max(0, attempt - 1))
    return base * (1 + random.uniform(-jitter, jitter)) if jitter else base


async def reverse_ws_loop(gw: Gateway, *, session: aiohttp.ClientSession | None = None) -> None:
    """Keep a Universal reverse WebSocket to AstrBot; reconnect with exponential backoff."""
    url = gw.cfg.reverse_url
    if not url:
        log.info("reverse websocket disabled (ONEBOT_REVERSE_URL is empty)")
        return
    own = session is None
    session = session or aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_connect=10))
    attempt = 0
    try:
        while True:
            headers = {
                "X-Self-ID": str(gw.self_id),
                "X-Client-Role": "Universal",
                "User-Agent": f"wechat-api/{__version__}",
            }
            if gw.cfg.reverse_token:
                headers["Authorization"] = f"Bearer {gw.cfg.reverse_token}"
            gw.state.reverse_attempts += 1
            connected_at = None
            try:
                async with session.ws_connect(url, headers=headers, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    connected_at = time.monotonic()
                    gw.state.reverse_connected = True
                    gw.state.reverse_retry_in = None
                    gw.state.reverse_last_error = ""
                    log.info("reverse websocket connected to %s", url)
                    await serve_ws(gw, ws, "reverse")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                gw.state.reverse_last_error = f"{type(exc).__name__}: {exc}"[:300]
            finally:
                gw.state.reverse_connected = False
            if connected_at is not None and time.monotonic() - connected_at > 30:
                attempt = 0  # it was a healthy connection; start over
            attempt += 1
            delay = backoff_delay(attempt, gw.cfg.reverse_max_backoff)
            gw.state.reverse_retry_in = round(delay, 1)
            log.warning("reverse websocket %s unavailable (%s); retry in %.1fs", url, gw.state.reverse_last_error or "closed", delay)
            await asyncio.sleep(delay)
    finally:
        if own:
            await session.close()
