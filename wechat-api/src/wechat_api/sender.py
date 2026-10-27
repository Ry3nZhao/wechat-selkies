"""Serial send queue: plan a OneBot message, type it into WeChat, verify it read back.

Nothing is ever re-sent automatically. A send that cannot be verified from the messages
read back through wx-cli is counted as failed and logged, to avoid flooding a chat.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .directory import member_display
from .parse import InboundMessage, normalize_text
from .status import State
from .store import Store, StoredMessage
from .ui import UiDriver, UiError

log = logging.getLogger(__name__)


class SendError(Exception):
    pass


@dataclass
class AtTarget:
    wxid: str
    display: str
    prefix: str


@dataclass
class SendPlan:
    chat_id: str
    is_group: bool
    ops: list[tuple[str, Any]] = field(default_factory=list)  # ("text", str) | ("at", AtTarget)
    quote: StoredMessage | None = None
    warnings: list[str] = field(default_factory=list)

    def expected_fragments(self) -> list[str]:
        frags = []
        for kind, value in self.ops:
            if kind == "text":
                t = normalize_text(value)
                if t:
                    frags.append(t)
            else:
                frags.append("@" + normalize_text(value.display))
        return frags


def quote_summary(msg: StoredMessage, limit: int = 30) -> str:
    text = normalize_text(msg.content)
    return text if len(text) <= limit else text[:limit] + "…"


def degraded_quote_text(msg: StoredMessage) -> str:
    who = msg.sender_name or "对方"
    return f"「{who}：{quote_summary(msg)}」\n"


def mention_prefix(target: str, others: list[str], max_len: int) -> str | None:
    """Shortest prefix (at least ``max_len`` chars, or the full name) that no other member's
    name starts with; None when even the full name is a prefix of someone else's name."""
    n = min(len(target), max(1, max_len))
    while True:
        prefix = target[:n]
        if not any(o.startswith(prefix) for o in others):
            return prefix
        if n >= len(target):
            return None
        n += 1


def build_plan(
    segments: list[dict[str, Any]],
    *,
    chat_id: str,
    is_group: bool,
    store: Store,
    members: list[dict[str, Any]] | None,
    prefix_max_len: int = 8,
) -> SendPlan:
    plan = SendPlan(chat_id=chat_id, is_group=is_group)
    by_wxid = {str(m.get("username")): m for m in members or []}
    displays = {wxid: member_display(m) for wxid, m in by_wxid.items()}

    def add_text(text: str) -> None:
        if not text:
            return
        if plan.ops and plan.ops[-1][0] == "text":
            plan.ops[-1] = ("text", plan.ops[-1][1] + text)
        else:
            plan.ops.append(("text", text))

    for seg in segments:
        kind, data = seg["type"], seg.get("data") or {}
        if kind == "text":
            add_text(str(data.get("text", "")))
        elif kind == "reply":
            if plan.quote is not None:
                continue
            try:
                quoted = store.get_message(int(str(data.get("id", "0"))))
            except ValueError:
                quoted = None
            if quoted is None or quoted.chat_id != chat_id:
                plan.warnings.append(f"引用的消息 {data.get('id')} 不在本会话的记录里，忽略引用")
            else:
                plan.quote = quoted
        elif kind == "at":
            target = str(data.get("qq", ""))
            if not is_group:
                plan.warnings.append("私聊里不能 @，忽略")
                continue
            if target == "all":
                plan.warnings.append("@所有人 按普通文字发送")
                add_text("@所有人 ")
                continue
            wxid = store.raw_for(target, "user") or ""
            member = by_wxid.get(wxid)
            if member is None:
                name = str(data.get("name") or wxid or target)
                plan.warnings.append(f"@{name} 不在群成员表里，按普通文字发送")
                add_text(f"@{name} ")
                continue
            display = displays[wxid]
            others = [d for w, d in displays.items() if w != wxid and d]
            prefix = mention_prefix(display, others, prefix_max_len)
            if prefix is None:
                plan.warnings.append(f"@{display} 与其他成员重名或互为前缀，按普通文字发送")
                add_text(f"@{display} ")
                continue
            plan.ops.append(("at", AtTarget(wxid, display, prefix)))
        elif kind == "image":
            plan.warnings.append("暂不支持发图片，已忽略图片段")
        elif kind in {"face", "record", "video"}:
            plan.warnings.append(f"暂不支持发 {kind}，已忽略")
        else:
            plan.warnings.append(f"未知消息段 {kind}，已忽略")
    if not any(k == "at" or (k == "text" and v.strip()) for k, v in plan.ops):
        raise SendError("消息里没有可以发送的内容（目前只支持文字、@、引用）")
    return plan


def matches(plan: SendPlan, msg: InboundMessage, quote_native: bool) -> bool:
    """Does a message read back from WeChat correspond to what was typed?"""
    body = normalize_text(msg.text if msg.quote else msg.content)
    if not all(f in body for f in plan.expected_fragments()):
        return False
    if quote_native and plan.quote is not None:
        if msg.quote is None:
            return False
        want = normalize_text(plan.quote.content)
        got = normalize_text(msg.quote.text).removesuffix("...")
        if got and not (want.startswith(got) or got.startswith(want)):
            return False
    return True


@dataclass
class SendJob:
    plan: SendPlan
    chat_name: str
    message_id: int
    created: float = field(default_factory=time.time)
    quote_native: bool = False
    sent_at: float | None = None
    ui_done: asyncio.Future | None = None
    verified: asyncio.Future | None = None


class Verifier:
    """Matches messages read back from WeChat against recent sends."""

    def __init__(self) -> None:
        self.pending: list[SendJob] = []

    def add(self, job: SendJob) -> None:
        self.pending.append(job)

    def match(self, msg: InboundMessage, self_known: bool) -> SendJob | None:
        if self_known and not msg.is_self:
            return None
        for job in self.pending:
            if job.plan.chat_id != msg.chat_id or job.sent_at is None:
                continue
            if msg.timestamp and msg.timestamp < int(job.sent_at) - 5:
                continue
            if matches(job.plan, msg, job.quote_native):
                self.pending.remove(job)
                if job.verified and not job.verified.done():
                    job.verified.set_result(True)
                return job
        return None

    def expire(self, timeout: float, now: float | None = None) -> list[SendJob]:
        now = time.time() if now is None else now
        expired = [j for j in self.pending if j.sent_at is not None and now - j.sent_at > timeout]
        for job in expired:
            self.pending.remove(job)
            if job.verified and not job.verified.done():
                job.verified.set_result(False)
        return expired


class SendQueue:
    def __init__(
        self,
        ui: UiDriver,
        state: State,
        store: Store,
        verifier: Verifier,
        *,
        verify_timeout: float = 15.0,
        latest_in_chat: Callable[[str], StoredMessage | None] | None = None,
    ) -> None:
        self.ui = ui
        self.state = state
        self.store = store
        self.verifier = verifier
        self.verify_timeout = verify_timeout
        self.latest_in_chat = latest_in_chat or (lambda chat: next(iter(store.recent_in_chat(chat, 1)), None))
        self.queue: asyncio.Queue[SendJob] = asyncio.Queue()
        self.history: list[dict[str, Any]] = []  # step log of the last job, for tests/debugging

    async def submit(self, plan: SendPlan, chat_name: str) -> SendJob:
        loop = asyncio.get_running_loop()
        key = f"send|{plan.chat_id}|{time.time_ns()}"
        job = SendJob(plan, chat_name, self.store.allocate_message_id(key), ui_done=loop.create_future(), verified=loop.create_future())
        await self.queue.put(job)
        self.state.queue_size = self.queue.qsize()
        return job

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            self.state.queue_size = self.queue.qsize()
            try:
                await self.execute(job)
                if not job.ui_done.done():
                    job.ui_done.set_result(job.message_id)
            except Exception as exc:  # report and move on; never re-send
                self.state.send_failed += 1
                self.state.last_send = {"chat": job.plan.chat_id, "ok": False, "error": str(exc), "at": time.time()}
                log.error("send to %s failed: %s", job.plan.chat_id, exc)
                if not job.ui_done.done():
                    job.ui_done.set_exception(SendError(str(exc)))
                if job.verified and not job.verified.done():
                    job.verified.set_result(False)
            finally:
                self.queue.task_done()

    async def execute(self, job: SendJob) -> None:
        plan, ui = job.plan, self.ui
        if not self.state.logged_in:
            raise SendError("微信未登录或界面异常，暂停发送")
        win = await ui.main_window()
        try:
            await ui.activate(win)
            await ui.open_chat(job.chat_name)
            await ui.screenshot("opened")
            # Clear any leftover draft before typing.
            await ui.key("ctrl+a")
            await ui.key("BackSpace")
            if plan.quote is not None:
                await self._quote(job, win)
            for kind, value in plan.ops:
                if kind == "text":
                    await ui.paste(value)
                else:
                    await ui.insert_mention(value.prefix)
            await ui.screenshot("typed")
            job.sent_at = time.time()
            self.verifier.add(job)
            await ui.send()
            await ui.screenshot("sent")
        except UiError:
            await ui.cancel()
            raise
        for w in plan.warnings:
            log.warning("send to %s: %s", plan.chat_id, w)
        self.state.sent += 1
        self.state.last_send = {
            "chat": plan.chat_id,
            "ok": True,
            "verified": None,
            "message_id": job.message_id,
            "quote": "native" if job.quote_native else ("text" if plan.quote else None),
            "warnings": plan.warnings,
            "at": job.sent_at,
        }

    async def _quote(self, job: SendJob, win) -> None:
        plan, ui = job.plan, self.ui
        quoted = plan.quote
        latest = self.latest_in_chat(plan.chat_id)
        reason = ""
        if not ui.ui.get("quote", {}).get("enabled"):
            reason = "引用坐标未校准（quote.enabled=false）"
        elif latest is None or latest.message_id != quoted.message_id:
            reason = "被引用的消息不是会话里最新的一条，无法在界面上定位"
        else:
            try:
                await ui.quote_last(win, quoted.is_self)
                job.quote_native = True
                await ui.screenshot("quoted")
                return
            except UiError as exc:
                reason = f"界面引用失败：{exc}"
        log.warning("send to %s: 引用降级为文字（%s）", plan.chat_id, reason)
        plan.warnings.append(f"引用降级为文字：{reason}")
        plan.ops.insert(0, ("text", degraded_quote_text(quoted)))
