"""The gateway core: receive loop, status loop, event fan-out and OneBot actions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__
from .config import Config
from .cq import normalize_message
from .directory import Directory, member_display, member_names
from .events import build_event, build_segments, meta_event, stored_text
from .parse import InboundMessage, assign_keys, convert
from .sender import SendError, SendQueue, Verifier, build_plan
from .status import (
    LOGIN_UNKNOWN,
    READER_ERROR,
    READER_NEEDS_INIT,
    READER_OK,
    READER_UNKNOWN,
    State,
    find_process,
)
from .store import Store, StoredMessage
from .ui import UiDriver, UiError
from .wxcli import WxCli, WxError

log = logging.getLogger(__name__)

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class ActionError(Exception):
    def __init__(self, message: str, retcode: int = 100) -> None:
        super().__init__(message)
        self.retcode = retcode


def ok(data: Any = None) -> dict[str, Any]:
    return {"status": "ok", "retcode": 0, "data": data, "message": "", "wording": ""}


def failed(message: str, retcode: int = 100) -> dict[str, Any]:
    return {"status": "failed", "retcode": retcode, "data": None, "message": message, "wording": message}


def guess_self_wxid(wx_home: str) -> str:
    """Best-effort own wxid from the data directory wx-cli was configured with
    (``.../xwechat_files/<wxid>_<4 chars>/db_storage``). WECHAT_SELF_WXID overrides it."""
    try:
        data = json.loads((Path(wx_home) / ".wx-cli" / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    parts = Path(str(data.get("db_dir", ""))).parts
    if "db_storage" not in parts:
        return ""
    idx = parts.index("db_storage")
    if idx == 0:
        return ""
    folder = parts[idx - 1]
    return re.sub(r"_[0-9a-zA-Z]{4}$", "", folder) if folder.startswith("wxid_") else folder


class Gateway:
    def __init__(
        self,
        cfg: Config,
        *,
        store: Store | None = None,
        wx: WxCli | None = None,
        ui: UiDriver | None = None,
        state: State | None = None,
        proc_dir: str = "/proc",
    ) -> None:
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.wx = wx or WxCli(cfg.wx_bin, timeout=cfg.wx_timeout, home=cfg.wx_home)
        self.state = state or State()
        self.state.reverse_url = cfg.reverse_url
        self.directory = Directory(self.wx)
        self.ui = ui
        self.verifier = Verifier()
        self.sender = SendQueue(ui, self.state, self.store, self.verifier, verify_timeout=cfg.send_verify_timeout) if ui else None
        self.proc_dir = proc_dir
        self.sinks: set[EventSink] = set()
        self._first_poll = True
        self._self_file = Path(cfg.state_dir) / "self.json"
        self.state.self_wxid = cfg.self_wxid or self._load_self() or guess_self_wxid(cfg.wx_home)
        self.state.self_nickname = cfg.self_nickname

    # ---- identity ----------------------------------------------------------------------

    @property
    def self_id(self) -> int:
        return self.store.id_for(self.state.self_wxid, "user") if self.state.self_wxid else 0

    def _load_self(self) -> str:
        try:
            return str(json.loads(self._self_file.read_text("utf-8")).get("wxid", ""))
        except (OSError, ValueError):
            return ""

    def learn_self(self, wxid: str) -> None:
        if not wxid or wxid == self.state.self_wxid or self.cfg.self_wxid:
            return
        log.info("learned own wxid from a verified send")
        self.state.self_wxid = wxid
        try:
            self._self_file.parent.mkdir(parents=True, exist_ok=True)
            self._self_file.write_text(json.dumps({"wxid": wxid}), "utf-8")
        except OSError as exc:
            log.warning("cannot persist own wxid: %s", exc)

    def self_names(self, group: str) -> list[str]:
        names = [self.state.self_nickname]
        for m in self.directory.cached_members(group) or []:
            if m.get("username") == self.state.self_wxid:
                names += [str(m.get("group_nickname") or ""), str(m.get("display") or ""), str(m.get("contact_display") or "")]
        return [n for n in names if n]

    # ---- events ------------------------------------------------------------------------

    async def emit(self, event: dict[str, Any]) -> None:
        for sink in list(self.sinks):
            try:
                await sink(event)
            except Exception as exc:
                log.warning("event sink failed: %s", exc)

    # ---- receiving ---------------------------------------------------------------------

    async def check_reader(self) -> None:
        """Classify wx-cli's readiness with ``wx doctor`` (read-only; never repairs)."""
        result = await self.wx.doctor()
        self.state.reader_checked_at = time.time()
        self.state.reader_failed_checks = result.failed
        self.state.reader_detail = result.detail
        if result.ok:
            self.state.reader = READER_UNKNOWN if self.state.reader != READER_OK else READER_OK
        elif result.needs_user_init:
            if self.state.reader != READER_NEEDS_INIT:
                log.warning("wx-cli needs the user to initialise it; receiving paused")
            self.state.reader = READER_NEEDS_INIT
        else:
            self.state.reader = READER_ERROR

    async def poll_once(self) -> int:
        """One ``wx new-messages`` round; returns the number of events emitted."""
        data = await self.wx.new_messages()
        self.state.last_poll_at = time.time()
        self.state.reader = READER_OK
        self.state.reader_detail = "ok"
        unknown = (data.get("meta") or {}).get("unknown_shards") or []
        self.state.reader_warnings = (
            [f"有 {len(unknown)} 个消息分片读不了，结果可能不完整；需要用户按 wx-cli-again 的文档补全初始化"] if unknown else []
        )
        rows = [r for r in data.get("messages") or [] if isinstance(r, dict)]
        rows.sort(key=lambda r: int(r.get("timestamp") or 0))
        messages = assign_keys(convert(r, self.state.self_wxid) for r in rows)
        first, self._first_poll = self._first_poll, False
        emitted = 0
        for msg in messages:
            if await self.handle_message(msg, backlog=first):
                emitted += 1
        for job in self.verifier.expire(self.cfg.send_verify_timeout):
            self._verify_failed(job)
        return emitted

    def _verify_failed(self, job) -> None:
        self.state.verify_failed += 1
        log.error("send to %s not found when reading back (message_id %s); not re-sending", job.plan.chat_id, job.message_id)
        if self.state.last_send and self.state.last_send.get("message_id") == job.message_id:
            self.state.last_send["verified"] = False

    async def handle_message(self, msg: InboundMessage, backlog: bool = False) -> bool:
        if self.store.has_key(msg.key):
            return False
        self.directory.note_chat(msg.chat_id, msg.chat_name, msg.is_group)
        job = self.verifier.match(msg, self_known=bool(self.state.self_wxid))
        if job is not None:
            msg.is_self = True
            if msg.is_group:
                self.learn_self(msg.sender_id)
            if self.state.last_send and self.state.last_send.get("message_id") == job.message_id:
                self.state.last_send["verified"] = True
            message_id = job.message_id
        else:
            message_id = self.store.allocate_message_id(msg.key)
        if msg.timestamp:
            self.state.last_message_at = max(self.state.last_message_at or 0, msg.timestamp)

        if msg.is_group and "@" in msg.text and self.directory.cached_members(msg.chat_id) is None:
            try:
                await self.directory.members(msg.chat_id)
            except WxError as exc:
                log.warning("wx members %s failed: %s", msg.chat_id, exc)
        names = member_names(self.directory.cached_members(msg.chat_id) or []) if msg.is_group else {}
        image_path = await self._image_path(msg, message_id) if msg.image_local_id is not None else None
        segments = build_segments(
            msg, self.store, self_id=self.self_id, mention_names=names,
            self_names=self.self_names(msg.chat_id) if msg.is_group else (), image_path=image_path,
        )
        event = build_event(msg, self.store, message_id=message_id, self_id=self.self_id, segments=segments)
        if not self.store.put_message(
            StoredMessage(message_id, msg.key, msg.chat_id, msg.timestamp, msg.sender_id, msg.sender_name, stored_text(msg), msg.is_self, event)
        ):
            return False
        if msg.is_self:
            return False
        if backlog or (msg.timestamp and time.time() - msg.timestamp > self.cfg.max_backlog_seconds):
            return False  # stored for get_msg / quotes, but too old to answer
        self.state.received += 1
        await self.emit(event)
        return True

    async def _image_path(self, msg: InboundMessage, message_id: int) -> str | None:
        if not self.cfg.image_extract:
            return None
        try:
            for item in await self.wx.attachments(msg.chat_id, 30):
                if int(item.get("local_id") or -1) == msg.image_local_id and item.get("attachment_id"):
                    self.cfg.image_dir.mkdir(parents=True, exist_ok=True)
                    out = str(self.cfg.image_dir / f"{message_id}.jpg")
                    report = await self.wx.extract(str(item["attachment_id"]), out)
                    return str(report.get("output") or out)
        except (WxError, ValueError, OSError) as exc:
            log.info("image %s/%s not extracted: %s", msg.chat_id, msg.image_local_id, exc)
        return None

    async def receive_loop(self) -> None:
        backoff = self.cfg.poll_interval
        while True:
            if self.state.reader == READER_NEEDS_INIT:
                # Receiving stays paused until the user has initialised wx-cli by hand.
                await asyncio.sleep(self.cfg.recheck_interval)
                await self.check_reader()
                continue
            try:
                await self.poll_once()
                backoff = self.cfg.poll_interval
            except WxError as exc:
                log.warning("wx new-messages failed: %s", exc)
                await self.check_reader()
                if self.state.reader != READER_NEEDS_INIT:
                    self.state.reader = READER_ERROR
                    self.state.reader_detail = str(exc)[:300]
                backoff = min(max(backoff * 2, self.cfg.poll_interval), 30.0)
            except Exception:
                log.exception("receive loop error")
                backoff = min(max(backoff * 2, self.cfg.poll_interval), 30.0)
            await asyncio.sleep(backoff)

    # ---- status ------------------------------------------------------------------------

    async def refresh_status(self) -> None:
        self.state.wechat_pid = find_process(self.cfg.wechat_process_patterns, self.proc_dir)
        if self.ui is None:
            self.state.login = LOGIN_UNKNOWN
        else:
            try:
                self.state.login, self.state.window_title = await self.ui.detect_login()
            except UiError as exc:
                self.state.login, self.state.window_title = LOGIN_UNKNOWN, str(exc)

    async def status_loop(self) -> None:
        while True:
            try:
                await self.refresh_status()
            except Exception:
                log.exception("status loop error")
            await asyncio.sleep(self.cfg.status_interval)

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.heartbeat_interval)
            await self.emit(
                meta_event("heartbeat", self.self_id, status=self.state.onebot_status(), interval=int(self.cfg.heartbeat_interval * 1000))
            )

    # ---- actions -----------------------------------------------------------------------

    async def call(self, action: str, params: dict[str, Any] | None) -> dict[str, Any]:
        params = params or {}
        action = action.strip("/").removesuffix("_async")
        handler = getattr(self, f"act_{action}", None)
        if handler is None:
            return failed(f"不支持的动作 {action}", 1404)
        try:
            return ok(await handler(params))
        except ActionError as exc:
            return failed(str(exc), exc.retcode)
        except SendError as exc:
            return failed(str(exc), 103)
        except WxError as exc:
            return failed(f"wx-cli 读取失败：{exc}", 103)
        except (TypeError, ValueError) as exc:
            return failed(f"参数错误：{exc}", 100)

    def _group(self, params: dict[str, Any]) -> str:
        raw = self.store.raw_for(params.get("group_id"), "group")
        if not raw:
            raise ActionError("未知的 group_id", 100)
        return raw

    def _user(self, params: dict[str, Any]) -> str:
        raw = self.store.raw_for(params.get("user_id"), "user")
        if not raw:
            raise ActionError("未知的 user_id", 100)
        return raw

    async def act_send_private_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send(self._user(params), False, params)

    async def act_send_group_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        return await self._send(self._group(params), True, params)

    async def act_send_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = params.get("message_type") or ("group" if params.get("group_id") else "private")
        if kind == "group":
            return await self.act_send_group_msg(params)
        return await self.act_send_private_msg(params)

    async def _send(self, chat_id: str, is_group: bool, params: dict[str, Any]) -> dict[str, Any]:
        if self.sender is None:
            raise ActionError("发送功能未启用", 103)
        if not self.state.logged_in:
            raise ActionError("微信未登录或界面异常，暂停发送：" + self.state.summary(), 103)
        segments = normalize_message(params.get("message"), bool(params.get("auto_escape")))
        name = self.directory.name_of(chat_id)
        if name is None or self.directory.is_ambiguous(chat_id):
            try:
                await self.directory.refresh_sessions(force=True)
            except WxError as exc:
                log.warning("wx sessions failed: %s", exc)
            name = self.directory.name_of(chat_id)
        if name is None:
            raise ActionError("找不到这个会话（只能发给最近会话里出现过的联系人或群）", 100)
        if self.directory.is_ambiguous(chat_id):
            raise ActionError(f"有多个会话都叫「{name}」，无法用搜索准确定位，拒绝发送", 103)
        members = None
        if is_group and any(s["type"] == "at" for s in segments):
            members = await self.directory.members(chat_id, force=True)
        plan = build_plan(
            segments, chat_id=chat_id, is_group=is_group, store=self.store, members=members,
            prefix_max_len=int(self.ui.ui.get("at_prefix_max_len", 8)) if self.ui else 8,
        )
        job = await self.sender.submit(plan, name)
        message_id = await job.ui_done
        if self.cfg.send_wait_verify:
            if not await asyncio.wait_for(asyncio.shield(job.verified), self.cfg.send_verify_timeout + 10):
                raise ActionError("已经输入并发送，但读回时没有核对上；不会自动重发", 103)
        return {"message_id": message_id}

    async def act_get_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        msg = self.store.get_message(int(params.get("message_id")))
        if msg is None:
            raise ActionError("消息不在最近记录里", 100)
        if msg.event:
            event = dict(msg.event)
            return {
                "time": event.get("time"),
                "message_type": event.get("message_type"),
                "message_id": msg.message_id,
                "real_id": msg.message_id,
                "sender": event.get("sender"),
                "message": event.get("message"),
                "raw_message": event.get("raw_message"),
                "group_id": event.get("group_id"),
                "wechat": event.get("wechat"),
            }
        return {
            "time": msg.timestamp,
            "message_type": "group" if msg.chat_id.endswith("@chatroom") else "private",
            "message_id": msg.message_id,
            "real_id": msg.message_id,
            "sender": {"user_id": self.store.id_for(msg.sender_id) if msg.sender_id else 0, "nickname": msg.sender_name},
            "message": [{"type": "text", "data": {"text": msg.content}}],
            "raw_message": msg.content,
            "wechat": {"chat_id": msg.chat_id, "sender_id": msg.sender_id, "synthetic": True},
        }

    async def act_get_login_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "user_id": self.self_id,
            "nickname": self.state.self_nickname or self.state.self_wxid,
            "wechat": {"wxid": self.state.self_wxid or None, "logged_in": self.state.logged_in},
        }

    async def act_get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.state.onebot_status()

    async def act_get_version_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"app_name": "wechat-api", "app_version": __version__, "protocol_version": "v11", "wx_cli": self.state.wx_version}

    async def act_can_send_image(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    async def act_can_send_record(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    async def act_get_group_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        await self.directory.refresh_sessions(force=bool(params.get("no_cache")))
        return [
            {"group_id": self.store.id_for(u, "group"), "group_name": name, "member_count": 0, "max_member_count": 0, "wechat_id": u}
            for u, name in self.directory.groups()
        ]

    async def act_get_group_info(self, params: dict[str, Any]) -> dict[str, Any]:
        group = self._group(params)
        members = await self.directory.members(group, force=bool(params.get("no_cache")))
        return {
            "group_id": self.store.id_for(group, "group"),
            "group_name": self.directory.name_of(group) or group,
            "member_count": len(members),
            "max_member_count": 0,
            "wechat_id": group,
        }

    def _member(self, group: str, m: dict[str, Any]) -> dict[str, Any]:
        uid = str(m.get("username") or "")
        return {
            "group_id": self.store.id_for(group, "group"),
            "user_id": self.store.id_for(uid, "user"),
            "nickname": str(m.get("contact_display") or m.get("display") or uid),
            "card": str(m.get("group_nickname") or ""),
            "sex": "unknown",
            "age": 0,
            "area": "",
            "join_time": 0,
            "last_sent_time": 0,
            "level": "",
            "role": "owner" if m.get("is_owner") else "member",
            "unfriendly": False,
            "title": "",
            "title_expire_time": 0,
            "card_changeable": False,
            "wechat_id": uid,
            "wechat_group_id": group,
            "display": member_display(m),
        }

    async def act_get_group_member_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        group = self._group(params)
        members = await self.directory.members(group, force=bool(params.get("no_cache")))
        return [self._member(group, m) for m in members]

    async def act_get_group_member_info(self, params: dict[str, Any]) -> dict[str, Any]:
        group = self._group(params)
        user = self._user(params)
        members = await self.directory.members(group, force=bool(params.get("no_cache")))
        for m in members:
            if m.get("username") == user:
                return self._member(group, m)
        raise ActionError("不是这个群的成员", 100)

    # ---- lifecycle ---------------------------------------------------------------------

    async def start(self) -> list[asyncio.Task]:
        try:
            self.state.wx_version = await self.wx.version()
        except (WxError, ValueError) as exc:
            self.state.wx_version = ""
            self.state.reader = READER_ERROR
            self.state.reader_detail = f"wx-cli 不可用：{exc}"
        await self.refresh_status()
        await self.check_reader()
        tasks = [
            asyncio.create_task(self.receive_loop(), name="receive"),
            asyncio.create_task(self.status_loop(), name="status"),
            asyncio.create_task(self.heartbeat_loop(), name="heartbeat"),
        ]
        if self.sender is not None:
            tasks.append(asyncio.create_task(self.sender.run(), name="sender"))
        return tasks
