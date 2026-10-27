"""Stable numeric ids for WeChat ids, and the recent-message store behind get_msg.

OneBot v11 wants integer ids. WeChat ids are strings (``xxx@chatroom``, ``wxid_xxx``), so
each one maps to a stable hash; the mapping is persisted so actions can turn a number back
into the WeChat id. wx-cli does not expose message ids, so messages are keyed by a dedup
key built from chat, time, sender and a hash of the content.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ID_BITS = 53  # stays exact in JavaScript clients
MESSAGE_ID_BITS = 31  # OneBot v11 message_id is int32
KEEP_MESSAGES = 10000


def stable_id(raw: str, bits: int = ID_BITS, salt: str = "wechat") -> int:
    digest = hashlib.sha256(f"{salt}:{raw}".encode("utf-8")).digest()
    value = int.from_bytes(digest[:8], "big") & ((1 << bits) - 1)
    return value or 1


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def dedup_key(chat: str, timestamp: int, sender: str, content: str) -> str:
    return f"{chat}|{timestamp}|{sender}|{content_hash(content)}"


@dataclass
class StoredMessage:
    message_id: int
    dedup_key: str
    chat_id: str
    timestamp: int
    sender_id: str
    sender_name: str
    content: str
    is_self: bool
    event: dict[str, Any] | None


class Store:
    """SQLite-backed id registry and message store. Thread-safe, synchronous, small."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS ids (
                    kind TEXT NOT NULL, num INTEGER NOT NULL, raw TEXT NOT NULL,
                    PRIMARY KEY (kind, num));
                CREATE UNIQUE INDEX IF NOT EXISTS ids_raw ON ids (kind, raw);
                CREATE TABLE IF NOT EXISTS messages (
                    message_id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE,
                    chat_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    sender_id TEXT NOT NULL, sender_name TEXT NOT NULL,
                    content TEXT NOT NULL, is_self INTEGER NOT NULL, event TEXT,
                    stored_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_chat ON messages (chat_id, timestamp);
                """
            )
            self._db.commit()

    # ---- id registry -------------------------------------------------------------------

    def id_for(self, raw: str, kind: str = "user") -> int:
        """Stable number for a WeChat id; ``kind`` is ``user`` or ``group``."""
        if not raw:
            return 0
        with self._lock:
            row = self._db.execute(
                "SELECT num FROM ids WHERE kind=? AND raw=?", (kind, raw)
            ).fetchone()
            if row:
                return int(row[0])
            num = stable_id(raw)
            # Collisions are astronomically unlikely at 53 bits; probe anyway so the
            # mapping always stays one-to-one.
            while self._db.execute(
                "SELECT 1 FROM ids WHERE kind=? AND num=?", (kind, num)
            ).fetchone():
                num = (num + 1) & ((1 << ID_BITS) - 1) or 1
            self._db.execute("INSERT INTO ids (kind, num, raw) VALUES (?,?,?)", (kind, num, raw))
            self._db.commit()
            return num

    def raw_for(self, value: Any, kind: str = "user") -> str | None:
        """WeChat id for a number (or a string that already is a WeChat id)."""
        if value is None or value == "":
            return None
        text = str(value).strip()
        if not text.lstrip("-").isdigit():
            return text  # already a WeChat id
        with self._lock:
            row = self._db.execute(
                "SELECT raw FROM ids WHERE kind=? AND num=?", (kind, int(text))
            ).fetchone()
        return row[0] if row else None

    # ---- messages ----------------------------------------------------------------------

    def has_key(self, key: str) -> bool:
        with self._lock:
            return (
                self._db.execute("SELECT 1 FROM messages WHERE dedup_key=?", (key,)).fetchone()
                is not None
            )

    def allocate_message_id(self, key: str) -> int:
        num = stable_id(key, MESSAGE_ID_BITS, salt="msg")
        with self._lock:
            while self._db.execute(
                "SELECT 1 FROM messages WHERE message_id=?", (num,)
            ).fetchone():
                num = (num + 1) & ((1 << MESSAGE_ID_BITS) - 1) or 1
        return num

    def put_message(self, msg: StoredMessage) -> bool:
        """Insert; returns False when the dedup key (or message id) is already stored."""
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO messages (message_id, dedup_key, chat_id, timestamp, sender_id,"
                    " sender_name, content, is_self, event, stored_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        msg.message_id,
                        msg.dedup_key,
                        msg.chat_id,
                        msg.timestamp,
                        msg.sender_id,
                        msg.sender_name,
                        msg.content,
                        int(msg.is_self),
                        json.dumps(msg.event, ensure_ascii=False) if msg.event else None,
                        time.time(),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            count = self._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            if count > KEEP_MESSAGES:
                self._db.execute(
                    "DELETE FROM messages WHERE message_id IN (SELECT message_id FROM messages"
                    " ORDER BY stored_at ASC LIMIT ?)",
                    (count - KEEP_MESSAGES,),
                )
            self._db.commit()
            return True

    def get_message(self, message_id: int) -> StoredMessage | None:
        with self._lock:
            row = self._db.execute(
                "SELECT message_id, dedup_key, chat_id, timestamp, sender_id, sender_name,"
                " content, is_self, event FROM messages WHERE message_id=?",
                (int(message_id),),
            ).fetchone()
        return _row(row) if row else None

    def recent_in_chat(self, chat_id: str, limit: int = 500) -> list[StoredMessage]:
        """Newest first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT message_id, dedup_key, chat_id, timestamp, sender_id, sender_name,"
                " content, is_self, event FROM messages WHERE chat_id=?"
                " ORDER BY timestamp DESC, stored_at DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return [_row(r) for r in rows]


def _row(row: tuple) -> StoredMessage:
    return StoredMessage(
        message_id=int(row[0]),
        dedup_key=row[1],
        chat_id=row[2],
        timestamp=int(row[3]),
        sender_id=row[4],
        sender_name=row[5],
        content=row[6],
        is_self=bool(row[7]),
        event=json.loads(row[8]) if row[8] else None,
    )
