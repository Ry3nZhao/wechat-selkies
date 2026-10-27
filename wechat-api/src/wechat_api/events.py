"""Build OneBot v11 message segments and events from inbound WeChat messages."""

from __future__ import annotations

import time
from typing import Any, Iterable

from .cq import to_cq
from .parse import InboundMessage, Quote, normalize_text, split_mentions
from .store import Store, StoredMessage, content_hash


def stored_text(msg: InboundMessage) -> str:
    """What a later quote of this message would show: the reply text, not the quote block."""
    if msg.image_local_id is not None:
        return "[图片]"
    return msg.text


def find_quoted(store: Store, chat_id: str, quote: Quote) -> StoredMessage | None:
    candidates = store.recent_in_chat(chat_id, 500)
    texts = [quote.text]
    if quote.sender:
        texts.append(f"{quote.sender}: {quote.text}")
    for require_sender in (True, False):
        for text in texts:
            want = normalize_text(text).removesuffix("...")
            if not want:
                continue
            for m in candidates:
                if require_sender and quote.sender and m.sender_name != quote.sender:
                    continue
                if normalize_text(m.content).startswith(want):
                    return m
    return None


def resolve_quote(store: Store, msg: InboundMessage) -> int:
    """message_id of the quoted message; unknown originals get a synthetic stored entry so
    that get_msg on the reply id still answers."""
    assert msg.quote is not None
    found = find_quoted(store, msg.chat_id, msg.quote)
    if found:
        return found.message_id
    key = f"quote|{msg.chat_id}|{msg.quote.sender}|{content_hash(msg.quote.text)}"
    mid = store.allocate_message_id(key)
    if not store.put_message(
        StoredMessage(mid, key, msg.chat_id, msg.timestamp, "", msg.quote.sender, msg.quote.text, False, None)
    ):
        # Already synthesised earlier: look it up again by text.
        found = find_quoted(store, msg.chat_id, msg.quote)
        if found:
            return found.message_id
    return mid


def build_segments(
    msg: InboundMessage,
    store: Store,
    *,
    self_id: int,
    mention_names: dict[str, str] | None = None,
    self_names: Iterable[str] = (),
    image_path: str | None = None,
) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    if msg.quote is not None:
        segments.append(
            {
                "type": "reply",
                "data": {
                    "id": str(resolve_quote(store, msg)),
                    "wechat_quote": {"sender": msg.quote.sender, "text": msg.quote.text},
                },
            }
        )
    if msg.image_local_id is not None:
        segments.append(
            {
                "type": "image",
                "data": {
                    "file": f"file://{image_path}" if image_path else f"wechat-local://{msg.chat_id}/{msg.image_local_id}",
                    "path": image_path,
                    "url": "",
                    "wechat_local_id": msg.image_local_id,
                },
            }
        )
        return segments
    if msg.is_group and "@" in msg.text:
        parts = split_mentions(msg.text, mention_names or {}, self_names)
    else:
        parts = [("text", msg.text)] if msg.text else []
    for kind, value in parts:
        if kind == "text":
            segments.append({"type": "text", "data": {"text": value}})
        elif value.wxid == "all":
            segments.append({"type": "at", "data": {"qq": "all", "name": value.name}})
        else:
            num = self_id if value.is_self else store.id_for(value.wxid, "user")
            segments.append(
                {"type": "at", "data": {"qq": str(num), "name": value.name, "wechat_id": value.wxid or None}}
            )
    if not segments:
        segments.append({"type": "text", "data": {"text": msg.content}})
    return segments


def build_event(msg: InboundMessage, store: Store, *, message_id: int, self_id: int, segments: list[dict[str, Any]]) -> dict[str, Any]:
    user_id = store.id_for(msg.sender_id, "user") if msg.sender_id else 0
    event: dict[str, Any] = {
        "time": msg.timestamp or int(time.time()),
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group" if msg.is_group else "private",
        "sub_type": "normal" if msg.is_group else "friend",
        "message_id": message_id,
        "user_id": user_id,
        "message": segments,
        "raw_message": to_cq(segments),
        "message_format": "array",
        "font": 0,
        "sender": {"user_id": user_id, "nickname": msg.sender_contact or msg.sender_name},
        "wechat": {
            "chat_id": msg.chat_id,
            "chat_name": msg.chat_name,
            "chat_type": msg.chat_type,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "type_code": msg.type_code,
            "appmsg_type": msg.appmsg_type,
            "content": msg.content,
        },
    }
    if msg.is_group:
        event["group_id"] = store.id_for(msg.chat_id, "group")
        event["anonymous"] = None
        event["sender"].update({"card": msg.sender_group_nickname or msg.sender_name, "role": "member"})
    return event


def meta_event(kind: str, self_id: int, **extra: Any) -> dict[str, Any]:
    event = {"time": int(time.time()), "self_id": self_id, "post_type": "meta_event", "meta_event_type": kind}
    event.update(extra)
    return event
