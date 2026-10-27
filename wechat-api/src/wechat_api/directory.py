"""Chat names and group members, cached from wx-cli's read commands."""

from __future__ import annotations

import logging
import time
from typing import Any

from .wxcli import WxCli, WxError

log = logging.getLogger(__name__)


class Directory:
    def __init__(self, wx: WxCli, *, members_ttl: float = 300.0, sessions_ttl: float = 300.0) -> None:
        self.wx = wx
        self.members_ttl = members_ttl
        self.sessions_ttl = sessions_ttl
        self.chats: dict[str, dict[str, Any]] = {}  # username -> {"name", "is_group"}
        self._members: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._sessions_at: float | None = None

    def note_chat(self, username: str, name: str, is_group: bool) -> None:
        if username:
            self.chats[username] = {"name": name or username, "is_group": is_group}

    async def refresh_sessions(self, force: bool = False) -> None:
        if not force and self._sessions_at is not None and time.monotonic() - self._sessions_at < self.sessions_ttl:
            return
        sessions = await self.wx.sessions(500)
        for s in sessions:
            self.note_chat(str(s.get("username") or ""), str(s.get("chat") or ""), bool(s.get("is_group")))
        self._sessions_at = time.monotonic()

    def name_of(self, username: str) -> str | None:
        entry = self.chats.get(username)
        return entry["name"] if entry else None

    def is_ambiguous(self, username: str) -> bool:
        name = self.name_of(username)
        return name is not None and any(
            u != username and c["name"] == name for u, c in self.chats.items()
        )

    def groups(self) -> list[tuple[str, str]]:
        return [(u, c["name"]) for u, c in self.chats.items() if c["is_group"]]

    async def members(self, group: str, force: bool = False) -> list[dict[str, Any]]:
        cached = self._members.get(group)
        if cached and not force and time.monotonic() - cached[0] < self.members_ttl:
            return cached[1]
        try:
            members = await self.wx.members(group)
        except WxError as exc:
            if cached:
                log.warning("wx members %s failed, using cache: %s", group, exc)
                return cached[1]
            raise
        self._members[group] = (time.monotonic(), members)
        return members

    def cached_members(self, group: str) -> list[dict[str, Any]] | None:
        cached = self._members.get(group)
        return cached[1] if cached else None


def member_display(member: dict[str, Any]) -> str:
    return str(member.get("group_nickname") or member.get("display") or member.get("contact_display") or member.get("username") or "")


def member_names(members: list[dict[str, Any]]) -> dict[str, str]:
    """Every name a member can be mentioned by -> wxid (group nickname wins on clashes)."""
    names: dict[str, str] = {}
    for m in members:
        uid = str(m.get("username") or "")
        for key in ("contact_display", "display", "group_nickname"):
            value = str(m.get(key) or "")
            if value and uid:
                names[value] = uid
    return names
