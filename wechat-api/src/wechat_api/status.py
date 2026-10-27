"""Gateway state: WeChat process, login window, wx-cli readability, counters."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

NEEDS_USER_INIT_HINT = "需要用户初始化：请在容器里按 wx-cli-again 自己的文档完成初始化"

# reader states
READER_UNKNOWN = "unknown"
READER_OK = "ok"
READER_NEEDS_INIT = "needs_user_init"
READER_ERROR = "error"

# login states
LOGIN_UNKNOWN = "unknown"
LOGIN_OK = "logged_in"
LOGIN_SCREEN = "login_screen"
LOGIN_NO_WINDOW = "no_window"


def find_process(patterns: Iterable[str], proc_dir: str | Path = "/proc") -> int | None:
    """PID of the first process whose command line starts with one of ``patterns``."""
    patterns = tuple(patterns)
    root = Path(proc_dir)
    try:
        entries = sorted((p for p in root.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    except OSError:
        return None
    for entry in entries:
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv0 = cmdline.split(b"\0", 1)[0].decode("utf-8", "replace")
        if argv0 and any(argv0 == p or argv0.startswith(p + " ") for p in patterns):
            return int(entry.name)
    return None


@dataclass
class State:
    started_at: float = field(default_factory=time.time)
    wechat_pid: int | None = None
    login: str = LOGIN_UNKNOWN
    window_title: str = ""
    reader: str = READER_UNKNOWN
    reader_detail: str = ""
    reader_failed_checks: list[str] = field(default_factory=list)
    reader_warnings: list[str] = field(default_factory=list)
    reader_checked_at: float | None = None
    last_poll_at: float | None = None
    last_message_at: float | None = None  # WeChat timestamp of the newest message seen
    received: int = 0
    sent: int = 0
    send_failed: int = 0
    verify_failed: int = 0
    last_send: dict[str, Any] | None = None
    queue_size: int = 0
    reverse_connected: bool = False
    reverse_url: str = ""
    reverse_attempts: int = 0
    reverse_retry_in: float | None = None
    reverse_last_error: str = ""
    ws_clients: int = 0
    self_wxid: str = ""
    self_nickname: str = ""
    wx_version: str = ""

    @property
    def wechat_running(self) -> bool:
        return self.wechat_pid is not None

    @property
    def logged_in(self) -> bool:
        return self.wechat_running and self.login == LOGIN_OK

    @property
    def needs_user_init(self) -> bool:
        return self.reader == READER_NEEDS_INIT

    @property
    def receiving(self) -> bool:
        return self.reader == READER_OK

    @property
    def online(self) -> bool:
        return self.logged_in and self.receiving

    def summary(self) -> str:
        if not self.wechat_running:
            return "微信没有运行"
        if self.login != LOGIN_OK:
            return "微信未登录：请打开 Selkies 网页扫码登录"
        if self.needs_user_init:
            return NEEDS_USER_INIT_HINT
        if self.reader == READER_ERROR:
            return "wx-cli 读取失败：" + self.reader_detail
        if self.reader != READER_OK:
            return "正在检查 wx-cli 读取"
        return "正常"

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.online,
            "summary": self.summary(),
            "needs_user_init": self.needs_user_init,
            "wechat_process": {"running": self.wechat_running, "pid": self.wechat_pid},
            "login": {"state": self.login, "window_title": self.window_title},
            "reader": {
                "state": self.reader,
                "detail": NEEDS_USER_INIT_HINT if self.needs_user_init else self.reader_detail,
                "failed_checks": self.reader_failed_checks,
                "warnings": self.reader_warnings,
                "checked_at": self.reader_checked_at,
                "wx_version": self.wx_version,
            },
            "receiving": self.receiving,
            "last_poll_at": self.last_poll_at,
            "last_message_at": self.last_message_at,
            "received": self.received,
            "sender": {
                "queue": self.queue_size,
                "sent": self.sent,
                "failed": self.send_failed,
                "verify_failed": self.verify_failed,
                "last": self.last_send,
            },
            "onebot": {
                "reverse_url": self.reverse_url,
                "reverse_connected": self.reverse_connected,
                "reverse_attempts": self.reverse_attempts,
                "reverse_retry_in": self.reverse_retry_in,
                "reverse_last_error": self.reverse_last_error,
                "ws_clients": self.ws_clients,
            },
            "uptime": round(time.time() - self.started_at, 1),
        }

    def onebot_status(self) -> dict[str, Any]:
        return {
            "online": self.online,
            "good": self.online,
            "app_initialized": True,
            "app_enabled": True,
            "app_good": True,
            "stat": {
                "message_received": self.received,
                "message_sent": self.sent,
                "send_failed": self.send_failed,
                "verify_failed": self.verify_failed,
            },
            "wechat": {
                "summary": self.summary(),
                "wechat_running": self.wechat_running,
                "login": self.login,
                "reader": self.reader,
                "needs_user_init": self.needs_user_init,
                "receiving": self.receiving,
                "last_message_at": self.last_message_at,
            },
        }


def display_env(display: str) -> dict[str, str]:
    env = dict(os.environ)
    env["DISPLAY"] = display
    return env
