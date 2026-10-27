from wechat_api.store import MESSAGE_ID_BITS, Store, StoredMessage, dedup_key, stable_id


def test_stable_id_is_deterministic_and_in_range():
    a = stable_id("12345678@chatroom")
    assert a == stable_id("12345678@chatroom")
    assert 0 < a < 2**53
    assert a != stable_id("wxid_abc")
    assert 0 < stable_id("x", MESSAGE_ID_BITS) < 2**31


def test_id_round_trip_and_persistence(tmp_path):
    path = tmp_path / "db.sqlite3"
    store = Store(path)
    gid = store.id_for("12345678@chatroom", "group")
    uid = store.id_for("wxid_abc", "user")
    assert gid == stable_id("12345678@chatroom")
    assert store.raw_for(gid, "group") == "12345678@chatroom"
    assert store.raw_for(str(uid), "user") == "wxid_abc"
    assert store.raw_for(gid, "user") is None  # kinds are separate
    assert store.raw_for("wxid_direct") == "wxid_direct"  # WeChat ids pass through
    assert store.raw_for(999) is None
    reopened = Store(path)
    assert reopened.raw_for(gid, "group") == "12345678@chatroom"


def test_message_dedup_and_lookup():
    store = Store()
    key = dedup_key("c", 1, "s", "hello")
    mid = store.allocate_message_id(key)
    msg = StoredMessage(mid, key, "c", 1, "s", "S", "hello", False, {"x": 1})
    assert store.put_message(msg)
    assert store.has_key(key)
    assert not store.put_message(msg)
    got = store.get_message(mid)
    assert got.content == "hello" and got.event == {"x": 1}
    assert store.recent_in_chat("c")[0].message_id == mid
    # A second message whose hash collides with a stored id gets probed to a free id.
    assert store.allocate_message_id(key) != mid
