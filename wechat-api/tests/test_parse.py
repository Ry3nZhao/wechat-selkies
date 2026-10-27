from conftest import fixture

from wechat_api.parse import Mention, assign_keys, convert, normalize_text, parse_quote, split_mentions


def rows():
    return fixture("new_messages.json")["messages"]


def test_group_text_uses_sender_identity():
    msg = convert(rows()[0], "wxid_self")
    assert msg.is_group and msg.chat_id == "12345678@chatroom"
    assert msg.sender_id == "wxid_xiaoming"
    assert msg.sender_name == "小明" and msg.sender_contact == "王小明"
    assert not msg.is_self
    assert msg.text == "大家好" and msg.quote is None


def test_private_peer_message_gets_peer_as_sender():
    msg = convert(rows()[4], "wxid_self")
    assert not msg.is_group
    assert msg.sender_id == "wxid_zhangsan"
    assert msg.sender_name == "张三"
    assert not msg.is_self


def test_own_messages_are_marked_self():
    assert convert(rows()[5], "wxid_self").is_self  # private: non-empty sender label
    assert convert(rows()[6], "wxid_self").is_self  # group: sender_username == own wxid
    assert not convert(rows()[6], "").is_self  # unknown own wxid: cannot tell in groups


def test_quote_message_is_split():
    msg = convert(rows()[2], "wxid_self")
    assert msg.text == "这个怎么弄"
    assert msg.quote is not None
    assert msg.quote.sender == "小明" and msg.quote.text == "大家好"


def test_parse_quote_variants():
    assert parse_quote("普通消息") == ("普通消息", None)
    reply, quote = parse_quote("[引用] 第一行\n第二行\n  ↳ 张三: a: b")
    assert reply == "第一行\n第二行"
    assert quote.sender == "张三" and quote.text == "a: b"
    reply, quote = parse_quote("[引用] 看这个\n  ↳ [图片]")
    assert reply == "看这个" and quote.sender == "" and quote.text == "[图片]"
    reply, quote = parse_quote("[引用] 只有标题")
    assert reply == "只有标题" and quote is None


def test_image_local_id():
    msg = convert(rows()[3], "wxid_self")
    assert msg.image_local_id == 42


def test_dedup_keys_are_stable_and_unique_in_batch():
    a = convert(rows()[0], "wxid_self")
    b = convert(rows()[0], "wxid_self")
    assert a.key == b.key
    keyed = assign_keys([a, b])
    assert keyed[0].key != keyed[1].key and keyed[1].key.endswith("#1")
    assert convert(rows()[1], "wxid_self").key != a.key


def test_split_mentions():
    names = {"小红": "wxid_xiaohong", "小红帽": "wxid_xiaohong2", "小明": "wxid_xiaoming"}
    parts = split_mentions("@洛洛 帮我看看 @小红帽 和 @小红 的问题 @所有人", names, ["洛洛"])
    ats = [p[1] for p in parts if p[0] == "at"]
    assert ats == [
        Mention("洛洛", "", True),
        Mention("小红帽", "wxid_xiaohong2"),
        Mention("小红", "wxid_xiaohong"),
        Mention("所有人", "all"),
    ]
    texts = [p[1] for p in parts if p[0] == "text"]
    assert texts[0] == "帮我看看 "


def test_split_mentions_leaves_unknown_and_inline_at_alone():
    parts = split_mentions("mail me@小明.com or @陌生人 hi", {"小明": "wxid_xiaoming"}, [])
    assert parts == [("text", "mail me@小明.com or @陌生人 hi")]


def test_normalize_text():
    assert normalize_text("@a  b\n c") == "@a b c"
