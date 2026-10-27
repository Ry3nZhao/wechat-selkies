import asyncio

from conftest import fixture

from wechat_api.status import READER_NEEDS_INIT, READER_OK
from wechat_api.store import stable_id


async def poll(gw, fake_wx, data=None):
    events = []

    async def sink(e):
        events.append(e)

    gw.sinks.add(sink)
    fake_wx.set("new-messages", data or fixture("new_messages.json"))
    await gw.poll_once()
    gw.sinks.discard(sink)
    return events


async def test_poll_converts_and_skips_own_messages(make_gateway, fake_wx):
    gw = make_gateway()
    events = await poll(gw, fake_wx)
    assert len(events) == 5  # 7 rows, 2 of them our own
    assert gw.state.reader == READER_OK
    assert gw.state.received == 5
    assert gw.state.last_message_at == 1761530030

    first = events[0]
    assert first["message_type"] == "group"
    assert first["group_id"] == stable_id("12345678@chatroom")
    assert first["user_id"] == stable_id("wxid_xiaoming")
    assert first["self_id"] == stable_id("wxid_self")
    assert first["sender"] == {"user_id": first["user_id"], "nickname": "王小明", "card": "小明", "role": "member"}
    assert first["message"] == [{"type": "text", "data": {"text": "大家好"}}]
    assert first["wechat"]["chat_id"] == "12345678@chatroom"
    assert first["wechat"]["sender_id"] == "wxid_xiaoming"
    # numeric ids map back to WeChat ids
    assert gw.store.raw_for(first["group_id"], "group") == "12345678@chatroom"


async def test_mentions_become_at_segments(make_gateway, fake_wx):
    gw = make_gateway()
    events = await poll(gw, fake_wx)
    msg = events[1]["message"]
    assert msg[0] == {"type": "at", "data": {"qq": str(stable_id("wxid_self")), "name": "洛洛", "wechat_id": "wxid_self"}}
    assert msg[1] == {"type": "text", "data": {"text": "帮我看看 "}}
    assert msg[2]["type"] == "at" and msg[2]["data"]["wechat_id"] == "wxid_xiaohong"
    assert events[1]["raw_message"].startswith(f"[CQ:at,qq={stable_id('wxid_self')},name=洛洛")


async def test_quote_becomes_reply_segment(make_gateway, fake_wx):
    gw = make_gateway()
    events = await poll(gw, fake_wx)
    quote_event = events[2]
    reply, text = quote_event["message"]
    assert reply["type"] == "reply"
    assert int(reply["data"]["id"]) == events[0]["message_id"]
    assert reply["data"]["wechat_quote"] == {"sender": "小明", "text": "大家好"}
    assert text == {"type": "text", "data": {"text": "这个怎么弄"}}
    got = await gw.call("get_msg", {"message_id": reply["data"]["id"]})
    assert got["data"]["message"] == events[0]["message"]


async def test_quote_of_unknown_message_is_synthesised(make_gateway, fake_wx):
    gw = make_gateway()
    row = dict(fixture("new_messages.json")["messages"][2], content="[引用] 啥\n  ↳ 老王: 很久以前的话")
    events = await poll(gw, fake_wx, {"messages": [row], "meta": {}})
    rid = events[0]["message"][0]["data"]["id"]
    got = await gw.call("get_msg", {"message_id": rid})
    assert got["status"] == "ok"
    assert got["data"]["message"][0]["data"]["text"] == "很久以前的话"
    assert got["data"]["wechat"]["synthetic"] is True


async def test_image_segment_has_local_reference(make_gateway, fake_wx):
    gw = make_gateway()
    events = await poll(gw, fake_wx)
    seg = events[3]["message"][0]
    assert seg["type"] == "image"
    assert seg["data"]["file"] == "wechat-local://12345678@chatroom/42"
    assert seg["data"]["wechat_local_id"] == 42


async def test_image_path_when_extracted(make_gateway, fake_wx, tmp_path):
    gw = make_gateway()
    gw.cfg.image_extract = True
    fake_wx.set("attachments", {"attachments": [{"attachment_id": "abc", "local_id": 42}]})
    out = str(tmp_path / "state" / "images" / "x.jpg")
    fake_wx.set("extract", {"output": out, "format": "jpg"})
    events = await poll(gw, fake_wx)
    seg = events[3]["message"][0]
    assert seg["data"]["path"] == out and seg["data"]["file"] == f"file://{out}"
    assert ["wx", "extract", "abc", "-o"] == fake_wx.calls[-1][:4]


async def test_private_message_event(make_gateway, fake_wx):
    gw = make_gateway()
    events = await poll(gw, fake_wx)
    priv = events[4]
    assert priv["message_type"] == "private" and priv["sub_type"] == "friend"
    assert priv["user_id"] == stable_id("wxid_zhangsan")
    assert "group_id" not in priv


async def test_repeated_rows_are_deduplicated(make_gateway, fake_wx):
    gw = make_gateway()
    assert len(await poll(gw, fake_wx)) == 5
    assert await poll(gw, fake_wx) == []


async def test_first_poll_backlog_is_stored_but_not_emitted(make_gateway, fake_wx):
    gw = make_gateway()
    gw._first_poll = True
    assert await poll(gw, fake_wx) == []
    assert gw.store.recent_in_chat("12345678@chatroom")


async def test_old_messages_are_not_emitted(make_gateway, fake_wx):
    gw = make_gateway(max_backlog_seconds=300)
    assert await poll(gw, fake_wx) == []  # fixture timestamps are long past


async def test_missing_setup_pauses_receiving(make_gateway, fake_wx):
    gw = make_gateway(poll_interval=0.01, recheck_interval=0.01)
    fake_wx.set("new-messages", None, code=1, err="config.json not found")
    fake_wx.set("doctor", fixture("doctor_missing.json"))
    task = asyncio.create_task(gw.receive_loop())
    await asyncio.sleep(0.2)
    assert gw.state.reader == READER_NEEDS_INIT
    polls = sum(1 for c in fake_wx.calls if c[1] == "new-messages")
    assert polls == 1  # paused after the first failure; only doctor rechecks
    health = gw.state.health()
    assert health["needs_user_init"] is True and health["receiving"] is False
    assert "需要用户初始化" in health["summary"]
    status = (await gw.call("get_status", {}))["data"]
    assert status["online"] is False and status["wechat"]["needs_user_init"] is True
    # once the user has initialised wx-cli, doctor passes and receiving resumes
    fake_wx.set("doctor", fixture("doctor_ok.json"))
    fake_wx.set("new-messages", {"messages": [], "meta": {}})
    await asyncio.sleep(0.2)
    task.cancel()
    assert gw.state.reader == READER_OK
    # the gateway only ever ran read-only commands
    assert {c[1] for c in fake_wx.calls} <= {"new-messages", "doctor"}


async def test_no_wx_config_means_user_init(make_gateway, fake_wx):
    gw = make_gateway()
    gw.wx_config_path.unlink()
    await gw.check_reader()
    assert gw.state.reader == READER_NEEDS_INIT
    assert gw.state.health()["reader"]["failed_checks"] == ["config.json"]
    assert not any(c[1] == "doctor" for c in fake_wx.calls)


async def test_unknown_shards_are_reported(make_gateway, fake_wx):
    gw = make_gateway()
    await poll(gw, fake_wx, {"messages": [], "meta": {"unknown_shards": ["message/message_3.db"]}})
    assert gw.state.reader == READER_OK
    assert gw.state.health()["reader"]["warnings"]
