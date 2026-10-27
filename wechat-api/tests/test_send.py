import asyncio

import pytest
from conftest import fixture

from wechat_api.parse import convert
from wechat_api.sender import SendError, build_plan, mention_prefix
from wechat_api.status import LOGIN_SCREEN
from wechat_api.store import Store, stable_id

GROUP = "12345678@chatroom"


async def prime(gw, fake_wx):
    """Read the fixture once so the gateway knows the chats, ids and messages."""
    fake_wx.set("new-messages", fixture("new_messages.json"))
    await gw.poll_once()
    fake_wx.set("new-messages", {"messages": [], "meta": {}})


def own_row(text, chat=GROUP, ts=None, quote=None):
    import time

    row = {
        "chat": "测试群", "username": chat, "is_group": chat.endswith("@chatroom"),
        "timestamp": ts or int(time.time()), "sender": "洛洛", "content": text, "type_code": 1,
    }
    if chat.endswith("@chatroom"):
        row["sender_username"] = "wxid_self"
    if quote:
        row.update(content=f"[引用] {text}\n  ↳ {quote}", type_code=49, appmsg_type=57)
    return row


def keys_pressed(fake_x):
    return [c[3] for c in fake_x.actions() if c[:2] == ["xdotool", "key"]]


# ---- planning --------------------------------------------------------------------------


def test_mention_prefix():
    assert mention_prefix("小红帽", ["小红", "小明"], 8) == "小红帽"
    assert mention_prefix("小红", ["小红帽"], 8) is None  # a prefix of someone else's name
    assert mention_prefix("Alexander", ["Alex", "Bob"], 2) == "Alexa"
    assert mention_prefix("Bob", ["Alex"], 1) == "B"


def test_plan_text_at_reply_and_unsupported():
    store = Store()
    members = fixture("members.json")
    store.id_for("wxid_xiaohong2", "user")
    segs = [
        {"type": "at", "data": {"qq": str(stable_id("wxid_xiaohong2"))}},
        {"type": "text", "data": {"text": " 你好"}},
        {"type": "image", "data": {"file": "x.png"}},
    ]
    plan = build_plan(segs, chat_id=GROUP, is_group=True, store=store, members=members)
    assert plan.ops[0][0] == "at" and plan.ops[0][1].prefix == "小红帽" and plan.ops[0][1].wxid == "wxid_xiaohong2"
    assert plan.ops[1] == ("text", " 你好")
    assert any("图片" in w for w in plan.warnings)
    assert plan.expected_fragments() == ["@小红帽", "你好"]


def test_plan_ambiguous_or_unknown_mention_degrades_to_text():
    store = Store()
    store.id_for("wxid_xiaohong", "user")
    segs = [
        {"type": "at", "data": {"qq": str(stable_id("wxid_xiaohong"))}},
        {"type": "at", "data": {"qq": "wxid_nobody", "name": "路人"}},
        {"type": "text", "data": {"text": "看下"}},
    ]
    plan = build_plan(segs, chat_id=GROUP, is_group=True, store=store, members=fixture("members.json"))
    assert plan.ops == [("text", "@小红 @路人 看下")]
    assert len(plan.warnings) == 2


def test_plan_rejects_empty_and_image_only():
    with pytest.raises(SendError):
        build_plan([{"type": "image", "data": {}}], chat_id=GROUP, is_group=True, store=Store(), members=[])
    with pytest.raises(SendError):
        build_plan([{"type": "text", "data": {"text": "  "}}], chat_id=GROUP, is_group=True, store=Store(), members=[])


# ---- executing -------------------------------------------------------------------------


async def test_send_text_command_sequence(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    res = await gw.call("send_group_msg", {"group_id": stable_id(GROUP), "message": "你好 &#91;世界&#93;"})
    runner.cancel()
    assert res["status"] == "ok" and res["data"]["message_id"] > 0
    acts = fake_x.actions()
    assert acts[0] == ["xdotool", "windowactivate", "--sync", "111"]
    assert keys_pressed(fake_x) == ["ctrl+f", "ctrl+v", "Return", "ctrl+a", "BackSpace", "ctrl+v", "Return"]
    assert fake_x.clipboard() == ["测试群", "你好 [世界]"]


async def test_send_native_at(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    gw.store.id_for("wxid_xiaohong2", "user")
    msg = [{"type": "at", "data": {"qq": str(stable_id("wxid_xiaohong2"))}}, {"type": "text", "data": {"text": " 收到"}}]
    res = await gw.call("send_msg", {"message_type": "group", "group_id": GROUP, "message": msg})
    runner.cancel()
    assert res["status"] == "ok"
    types = [c for c in fake_x.actions() if c[:2] == ["xdotool", "type"]]
    assert types == [["xdotool", "type", "--clearmodifiers", "--delay", "40", "--", "@"]]
    assert fake_x.clipboard() == ["测试群", "小红帽", " 收到"]
    assert keys_pressed(fake_x)[-4:] == ["ctrl+v", "Return", "ctrl+v", "Return"]


async def test_quote_degrades_to_text_when_not_calibrated(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    quoted = gw.store.recent_in_chat(GROUP)[-1]  # oldest: "大家好"
    res = await gw.call("send_group_msg", {"group_id": GROUP, "message": f"[CQ:reply,id={quoted.message_id}]好的"})
    runner.cancel()
    assert res["status"] == "ok"
    assert fake_x.clipboard()[1:] == ["「小明：大家好」\n", "好的"]
    assert not any(c[:2] == ["xdotool", "click"] for c in fake_x.actions())
    assert any("引用降级" in w for w in gw.state.last_send["warnings"])


async def test_native_quote_when_calibrated_and_latest(make_gateway, fake_wx, fake_x):
    gw = make_gateway({"quote": {"enabled": True, "menu_keys": ["Down", "Return"]}})
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    latest = gw.store.recent_in_chat(GROUP)[0]
    res = await gw.call("send_group_msg", {"group_id": GROUP, "message": [
        {"type": "reply", "data": {"id": str(latest.message_id)}}, {"type": "text", "data": {"text": "好"}}]})
    runner.cancel()
    assert res["status"] == "ok"
    acts = fake_x.actions()
    move = next(c for c in acts if c[:2] == ["xdotool", "mousemove"])
    # latest message is our own "收到": the self-side point of a 1000x800 window at (10, 20)
    assert move == ["xdotool", "mousemove", str(10 + 920), str(20 + int(800 * 0.78))]
    assert ["xdotool", "click", "3"] in acts
    assert keys_pressed(fake_x)[-4:] == ["Down", "Return", "ctrl+v", "Return"]
    assert gw.state.last_send["quote"] == "native"


async def test_sends_are_serial(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    r1, r2 = await asyncio.gather(
        gw.call("send_group_msg", {"group_id": GROUP, "message": "第一条"}),
        gw.call("send_private_msg", {"user_id": stable_id("wxid_zhangsan"), "message": "第二条"}),
    )
    runner.cancel()
    assert r1["status"] == r2["status"] == "ok"
    assert fake_x.clipboard() == ["测试群", "第一条", "张三", "第二条"]


async def test_verification_success_links_message_id(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    res = await gw.call("send_group_msg", {"group_id": GROUP, "message": "核对我"})
    mid = res["data"]["message_id"]
    fake_wx.set("new-messages", {"messages": [own_row("核对我")], "meta": {}})
    await gw.poll_once()
    runner.cancel()
    assert gw.state.last_send["verified"] is True
    assert gw.store.get_message(mid).content == "核对我"
    assert gw.state.verify_failed == 0


async def test_verification_learns_own_wxid(make_gateway, fake_wx, fake_x):
    gw = make_gateway(self_wxid="")
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    await gw.call("send_group_msg", {"group_id": GROUP, "message": "学一下"})
    fake_wx.set("new-messages", {"messages": [own_row("学一下")], "meta": {}})
    events = []
    gw.sinks.add(lambda e: events.append(e) or asyncio.sleep(0))
    await gw.poll_once()
    runner.cancel()
    assert gw.state.self_wxid == "wxid_self"
    assert events == []  # our own message is not echoed back as an event


async def test_verification_failure_is_counted_and_not_resent(make_gateway, fake_wx, fake_x):
    gw = make_gateway(send_verify_timeout=0.0)
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    await gw.call("send_group_msg", {"group_id": GROUP, "message": "原文"})
    fake_wx.set("new-messages", {"messages": [own_row("被改过的")], "meta": {}})
    await asyncio.sleep(0.01)
    await gw.poll_once()
    await asyncio.sleep(0.01)
    runner.cancel()
    assert gw.state.verify_failed == 1
    assert gw.state.last_send["verified"] is False
    assert keys_pressed(fake_x).count("Return") == 2  # one search confirm + one send; no retry


async def test_wait_for_verification_reports_failure(make_gateway, fake_wx, fake_x):
    gw = make_gateway(send_wait_verify=True, send_verify_timeout=0.05)
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    call = asyncio.create_task(gw.call("send_group_msg", {"group_id": GROUP, "message": "等核对"}))
    await asyncio.sleep(0.1)
    await gw.poll_once()  # nothing read back -> expires
    res = await call
    runner.cancel()
    assert res["status"] == "failed" and res["retcode"] == 103


async def test_quote_verification_requires_quote(make_gateway, fake_wx, fake_x):
    gw = make_gateway({"quote": {"enabled": True, "menu_keys": ["Return"]}})
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    latest = gw.store.recent_in_chat(GROUP)[0]
    await gw.call("send_group_msg", {"group_id": GROUP, "message": f"[CQ:reply,id={latest.message_id}]引用回复"})
    job = gw.verifier.pending[0]
    plain = convert(own_row("引用回复"), "wxid_self")
    assert gw.verifier.match(plain, True) is None  # text matches but the quote is missing
    quoted = convert(own_row("引用回复", quote="洛洛: 收到"), "wxid_self")
    assert gw.verifier.match(quoted, True) is job
    runner.cancel()


async def test_not_logged_in_refuses_to_send(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    gw.state.login = LOGIN_SCREEN
    res = await gw.call("send_group_msg", {"group_id": GROUP, "message": "hi"})
    assert res["status"] == "failed" and res["retcode"] == 103
    assert fake_x.actions() == []


async def test_ui_failure_is_reported_once(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await prime(gw, fake_wx)
    runner = asyncio.create_task(gw.sender.run())
    fake_x.fail_on = "xclip"
    res = await gw.call("send_group_msg", {"group_id": GROUP, "message": "hi"})
    runner.cancel()
    assert res["status"] == "failed" and res["retcode"] == 103
    assert gw.state.send_failed == 1
    assert sum(1 for c in fake_x.actions() if c[0] == "xclip") == 1


async def test_ambiguous_chat_name_is_refused(make_gateway, fake_wx, fake_x):
    gw = make_gateway()
    await gw.directory.refresh_sessions(force=True)
    res = await gw.call("send_private_msg", {"user_id": "wxid_a", "message": "hi"})
    assert res["status"] == "failed" and "同名" in res["message"]
    assert fake_x.actions() == []
