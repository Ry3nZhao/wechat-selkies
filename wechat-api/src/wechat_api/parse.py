"""Turn ``wx new-messages --json`` rows into normalised messages and OneBot segments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .store import dedup_key

QUOTE_PREFIX = "[引用]"
QUOTE_SEPARATOR = "\n  ↳ "
IMAGE_RE = re.compile(r"^\[图片\]\s*local_id=(\d+)\s*$")
MENTION_END = "  \t\n"


@dataclass
class Quote:
    text: str
    sender: str = ""  # display name as WeChat renders it in the quote line; may be empty


@dataclass
class InboundMessage:
    chat_id: str  # WeChat id of the conversation (wxid_xxx or xxx@chatroom)
    chat_name: str
    is_group: bool
    chat_type: str
    timestamp: int
    sender_id: str  # WeChat id of the sender; private chats fall back to the peer
    sender_name: str  # group nickname in groups, contact display otherwise
    sender_contact: str
    sender_group_nickname: str
    content: str  # wx-cli's rendered content
    type_code: int
    appmsg_type: int | None
    is_self: bool
    text: str  # content without the quote block
    quote: Quote | None = None
    image_local_id: int | None = None
    key: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def normalize_text(text: str) -> str:
    """Collapse whitespace (including the U+2005 WeChat puts after a mention)."""
    return " ".join(text.replace(" ", " ").split())


def parse_quote(content: str) -> tuple[str, Quote | None]:
    """Split ``[引用] reply\\n  ↳ sender: original`` into the reply text and the quote."""
    if not content.startswith(QUOTE_PREFIX):
        return content, None
    head, sep, tail = content.rpartition(QUOTE_SEPARATOR)
    if not sep:
        return content[len(QUOTE_PREFIX):].strip(), None
    reply = head[len(QUOTE_PREFIX):].strip()
    tail = tail.strip()
    sender, colon, original = tail.partition(": ")
    if colon and sender and "\n" not in sender and len(sender) <= 64:
        return reply, Quote(text=original.strip(), sender=sender.strip())
    return reply, Quote(text=tail)


def convert(row: dict[str, Any], self_wxid: str = "") -> InboundMessage:
    chat_id = str(row.get("username") or "")
    is_group = bool(row.get("is_group")) or chat_id.endswith("@chatroom")
    sender_label = str(row.get("sender") or "")
    sender_username = str(row.get("sender_username") or "")
    content = str(row.get("content") or "")
    type_code = int(row.get("type_code") or 0)
    appmsg = row.get("appmsg_type")

    if is_group:
        sender_id = sender_username
        is_self = bool(self_wxid) and sender_id == self_wxid
        if not sender_id and not sender_label:
            # Own messages carry no "wxid:\n" prefix; without an id mapping both are empty.
            is_self = True
            sender_id = self_wxid
    else:
        # wx-cli leaves ``sender`` empty for the peer's messages; a non-empty label in a
        # private chat is our own account.
        if sender_label:
            is_self = True
            sender_id = self_wxid
        else:
            is_self = False
            sender_id = chat_id
            sender_label = str(row.get("chat") or chat_id)

    text, quote = (content, None)
    if content.startswith(QUOTE_PREFIX) or (type_code == 49 and appmsg == 57):
        text, quote = parse_quote(content)

    image_local_id = None
    if type_code == 3:
        match = IMAGE_RE.match(content.strip())
        if match:
            image_local_id = int(match.group(1))

    timestamp = int(row.get("timestamp") or 0)
    return InboundMessage(
        chat_id=chat_id,
        chat_name=str(row.get("chat") or chat_id),
        is_group=is_group,
        chat_type=str(row.get("chat_type") or ("group" if is_group else "private")),
        timestamp=timestamp,
        sender_id=sender_id,
        sender_name=sender_label,
        sender_contact=str(row.get("sender_contact_display") or sender_label),
        sender_group_nickname=str(row.get("sender_group_nickname") or ""),
        content=content,
        type_code=type_code,
        appmsg_type=int(appmsg) if isinstance(appmsg, int) else None,
        is_self=is_self,
        text=text,
        quote=quote,
        image_local_id=image_local_id,
        key=dedup_key(chat_id, timestamp, sender_id or sender_label, content),
        raw=row,
    )


def assign_keys(messages: Iterable[InboundMessage]) -> list[InboundMessage]:
    """Make keys unique inside one batch: identical rows in the same second get #n."""
    seen: dict[str, int] = {}
    out = []
    for msg in messages:
        n = seen.get(msg.key, 0)
        seen[msg.key] = n + 1
        if n:
            msg.key = f"{msg.key}#{n}"
        out.append(msg)
    return out


@dataclass
class Mention:
    name: str
    wxid: str  # "" when the name is not resolved; "all" for @所有人
    is_self: bool = False


def split_mentions(text: str, names: dict[str, str], self_names: Iterable[str]) -> list[tuple[str, Any]]:
    """Split text into ("text", str) and ("at", Mention) parts.

    ``names`` maps display names (group nickname or contact name) to wxids; ``self_names``
    are the names our own account goes by in this group. A mention is ``@name`` followed by
    U+2005, whitespace or the end of the text; the longest matching name wins.
    """
    self_set = {n for n in self_names if n}
    candidates = sorted({*names.keys(), *self_set, "所有人"}, key=len, reverse=True)
    parts: list[tuple[str, Any]] = []
    buf = ""
    i = 0
    while i < len(text):
        if text[i] == "@":
            hit = None
            for name in candidates:
                end = i + 1 + len(name)
                if text.startswith(name, i + 1) and (end == len(text) or text[end] in MENTION_END):
                    hit = name
                    break
            if hit is not None:
                if buf:
                    parts.append(("text", buf))
                    buf = ""
                if hit == "所有人" and hit not in names:
                    parts.append(("at", Mention(hit, "all")))
                elif hit in self_set:
                    parts.append(("at", Mention(hit, names.get(hit, ""), True)))
                else:
                    parts.append(("at", Mention(hit, names[hit])))
                i += 1 + len(hit)
                if i < len(text) and text[i] == " ":
                    i += 1
                continue
        buf += text[i]
        i += 1
    if buf:
        parts.append(("text", buf))
    return parts
