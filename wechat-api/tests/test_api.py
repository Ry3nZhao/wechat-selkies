import asyncio
import json

import pytest
from aiohttp import web
from conftest import fixture

from wechat_api.cq import normalize_message, parse_cq, to_cq
from wechat_api.server import backoff_delay, build_app, reverse_ws_loop
from wechat_api.status import LOGIN_SCREEN
from wechat_api.store import stable_id
from wechat_api.wxcli import WxCli, evaluate_doctor


@pytest.fixture
async def client(aiohttp_client, make_gateway, fake_wx):
    gw = make_gateway()
    fake_wx.set("new-messages", fixture("new_messages.json"))
    await gw.poll_once()
    c = await aiohttp_client(build_app(gw))
    c.gw = gw
    return c


AUTH = {"Authorization": "Bearer secret"}


async def test_auth_required(client):
    assert (await client.post("/api/onebot/get_status")).status == 401
    assert (await client.post("/api/onebot/get_status", headers={"Authorization": "Bearer nope"})).status == 403
    assert (await client.post("/api/onebot/get_status", headers=AUTH)).status == 200
    assert (await client.post("/api/onebot/get_status", headers={"Authorization": "Token secret"})).status == 200
    assert (await client.get("/api/onebot/get_status?access_token=secret")).status == 200
    assert (await client.get("/api/onebot/ws")).status == 401


async def test_token_unset_closes_onebot(client):
    client.gw.cfg.access_token = ""
    assert (await client.post("/api/onebot/get_status", headers=AUTH)).status == 503
    assert (await client.get("/api/health")).status == 200


async def test_health_reports_state(client):
    client.gw.state.login = LOGIN_SCREEN
    body = await (await client.get("/api/health")).json()
    assert body["ok"] is False
    assert body["login"]["state"] == "login_screen"
    assert body["reader"]["state"] == "ok"
    assert body["last_message_at"] == 1761530030
    assert "扫码" in body["summary"]
    status = await (await client.post("/api/onebot/get_status", headers=AUTH)).json()
    assert status["data"]["online"] is False and status["data"]["wechat"]["login"] == "login_screen"


async def test_info_actions(client):
    info = await (await client.post("/api/onebot/get_login_info", headers=AUTH)).json()
    assert info["data"]["user_id"] == stable_id("wxid_self")
    assert info["data"]["nickname"] == "洛洛"

    groups = (await (await client.post("/api/onebot/get_group_list", headers=AUTH, json={})).json())["data"]
    assert {"group_id": stable_id("12345678@chatroom"), "group_name": "测试群", "member_count": 0,
            "max_member_count": 0, "wechat_id": "12345678@chatroom"} in groups

    gid = stable_id("12345678@chatroom")
    members = (await (await client.post("/api/onebot/get_group_member_list", headers=AUTH, json={"group_id": gid})).json())["data"]
    assert len(members) == 5
    owner = next(m for m in members if m["wechat_id"] == "wxid_xiaoming")
    assert owner["role"] == "owner" and owner["card"] == "小明" and owner["user_id"] == stable_id("wxid_xiaoming")

    one = await (await client.post("/api/onebot/get_group_member_info", headers=AUTH,
                                   json={"group_id": gid, "user_id": owner["user_id"]})).json()
    assert one["data"]["nickname"] == "王小明"
    bad = await (await client.post("/api/onebot/get_group_member_info", headers=AUTH,
                                   json={"group_id": 1, "user_id": owner["user_id"]})).json()
    assert bad["status"] == "failed" and bad["retcode"] == 100


async def test_unknown_action(client):
    resp = await client.post("/api/onebot/set_friend_add_request", headers=AUTH, json={})
    assert resp.status == 404
    assert (await resp.json())["retcode"] == 1404


async def test_forward_websocket(client):
    ws = await client.ws_connect("/api/onebot/ws", headers=AUTH)
    hello = await ws.receive_json()
    assert hello["meta_event_type"] == "lifecycle" and hello["sub_type"] == "connect"
    await ws.send_json({"action": "get_login_info", "params": {}, "echo": "e1"})
    reply = await ws.receive_json()
    assert reply["echo"] == "e1" and reply["data"]["user_id"] == stable_id("wxid_self")
    await client.gw.emit({"post_type": "message", "x": 1})
    assert (await ws.receive_json())["x"] == 1
    await ws.close()


async def test_login_detection_by_window_size():
    from wechat_api.config import DEFAULT_UI
    from wechat_api.ui import UiDriver

    from conftest import FakeX

    class Small(FakeX):
        async def __call__(self, argv, stdin, timeout, capture):
            if list(argv[:2]) == ["xdotool", "getwindowgeometry"]:
                return 0, "X=372\nY=194\nWIDTH=280\nHEIGHT=380\n", ""
            return await super().__call__(argv, stdin, timeout, capture)

    assert await UiDriver(DEFAULT_UI, Small("微信")).detect_login() == ("login_screen", "微信")
    assert await UiDriver(DEFAULT_UI, FakeX("微信")).detect_login() == ("logged_in", "微信")
    assert (await UiDriver(DEFAULT_UI, FakeX("")).detect_login())[0] == "no_window"


def test_backoff_is_exponential_and_capped():
    assert [backoff_delay(n, 60, jitter=0) for n in range(1, 9)] == [1, 2, 4, 8, 16, 32, 60, 60]


async def test_reverse_ws_retries_without_crashing(make_gateway):
    gw = make_gateway(reverse_max_backoff=0.01)
    gw.cfg.reverse_url = "ws://127.0.0.1:9/ws"  # nothing listens there
    task = asyncio.create_task(reverse_ws_loop(gw))
    await asyncio.sleep(0.5)
    assert not task.done()
    task.cancel()
    assert gw.state.reverse_attempts >= 2
    assert gw.state.reverse_connected is False
    assert gw.state.reverse_last_error


async def test_reverse_ws_handshake_and_actions(aiohttp_server, make_gateway):
    seen = {}
    got = asyncio.get_running_loop().create_future()

    async def astrbot(request):
        seen.update(request.headers)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = json.loads((await ws.receive()).data)
        await ws.send_json({"action": "get_status", "params": {}, "echo": 7})
        reply = json.loads((await ws.receive()).data)
        got.set_result((hello, reply))
        await ws.close()
        return ws

    app = web.Application()
    app.router.add_get("/ws", astrbot)
    server = await aiohttp_server(app)
    gw = make_gateway(reverse_max_backoff=0.01)
    gw.cfg.reverse_url = f"ws://127.0.0.1:{server.port}/ws"
    gw.cfg.reverse_token = "astr"
    task = asyncio.create_task(reverse_ws_loop(gw))
    hello, reply = await asyncio.wait_for(got, 5)
    task.cancel()
    assert seen["X-Self-ID"] == str(stable_id("wxid_self"))
    assert seen["X-Client-Role"] == "Universal"
    assert seen["Authorization"] == "Bearer astr"
    assert hello["post_type"] == "meta_event"
    assert reply["echo"] == 7 and reply["status"] == "ok"


# ---- helpers -----------------------------------------------------------------------------


def test_cq_round_trip():
    segs = parse_cq("hi [CQ:at,qq=123] &#91;x&#93; [CQ:reply,id=5]")
    assert segs == [
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "text", "data": {"text": " [x] "}},
        {"type": "reply", "data": {"id": "5"}},
    ]
    assert to_cq(segs) == "hi [CQ:at,qq=123] &#91;x&#93; [CQ:reply,id=5]"
    assert normalize_message("[CQ:at,qq=1]", auto_escape=True) == [{"type": "text", "data": {"text": "[CQ:at,qq=1]"}}]


async def test_wxcli_refuses_non_read_commands():
    calls = []

    async def runner(argv, timeout):
        calls.append(argv)
        return 0, "{}", ""

    wx = WxCli("wx", runner=runner)
    for forbidden in ("init", "key", "daemon", "export"):
        with pytest.raises(ValueError):
            await wx._run(forbidden)
    assert calls == []


def test_doctor_evaluation():
    assert evaluate_doctor(fixture("doctor_ok.json")).ok
    missing = evaluate_doctor(fixture("doctor_missing.json"))
    assert missing.needs_user_init and missing.failed == ["config.json", "数据库密钥"]
    assert not evaluate_doctor({}).ok and not evaluate_doctor({}).needs_user_init
