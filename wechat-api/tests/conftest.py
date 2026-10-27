from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from wechat_api.config import DEFAULT_UI, Config, merge_ui
from wechat_api.gateway import Gateway
from wechat_api.status import LOGIN_OK, State
from wechat_api.store import Store
from wechat_api.ui import UiDriver
from wechat_api.wxcli import WxCli

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeWx:
    """Scripted wx binary: maps the subcommand to (code, stdout, stderr)."""

    def __init__(self) -> None:
        self.responses: dict[str, list[tuple[int, str, str]]] = {}
        self.calls: list[list[str]] = []

    def set(self, sub: str, data: Any = None, code: int = 0, err: str = "") -> None:
        out = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.responses[sub] = [(code, out, err)]

    def queue(self, sub: str, data: Any = None, code: int = 0, err: str = "") -> None:
        out = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        self.responses.setdefault(sub, []).append((code, out, err))

    async def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        sub = argv[1]
        options = self.responses.get(sub)
        if not options:
            return 1, "", f"no fake for {sub}"
        return options.pop(0) if len(options) > 1 else options[0]


class FakeX:
    """Records X commands; answers window queries with one logged-in main window."""

    def __init__(self, title: str = "Weixin") -> None:
        self.title = title
        self.calls: list[tuple[list[str], str | None]] = []
        self.fail_on: str | None = None

    async def __call__(self, argv, stdin, timeout, capture):
        argv = list(argv)
        self.calls.append((argv, stdin))
        if self.fail_on and self.fail_on in argv:
            return 1, "", "boom"
        if argv[:2] == ["xdotool", "search"]:
            return (0, "111\n", "") if self.title else (1, "", "")
        if argv[:2] == ["xdotool", "getwindowname"]:
            return 0, self.title + "\n", ""
        if argv[:2] == ["xdotool", "getwindowgeometry"]:
            return 0, "WINDOW=111\nX=10\nY=20\nWIDTH=1000\nHEIGHT=800\nSCREEN=0\n", ""
        return 0, "", ""

    def actions(self) -> list[list[str]]:
        """Commands that change the UI (window queries filtered out)."""
        return [
            c for c, _ in self.calls
            if c[:2] not in (["xdotool", "search"], ["xdotool", "getwindowname"], ["xdotool", "getwindowgeometry"])
        ]

    def clipboard(self) -> list[str]:
        return [s for c, s in self.calls if c[0] == "xclip"]


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def fake_wx() -> FakeWx:
    wx = FakeWx()
    wx.set("--version", "wx 0.6.3\n")
    wx.set("doctor", fixture("doctor_ok.json"))
    wx.set("members", fixture("members.json"))
    wx.set("sessions", fixture("sessions.json"))
    wx.set("attachments", {"attachments": []})
    return wx


@pytest.fixture
def fake_x() -> FakeX:
    return FakeX()


@pytest.fixture
def make_gateway(tmp_path, fake_wx, fake_x):
    def make(ui_overrides: dict | None = None, **cfg_kwargs) -> Gateway:
        # wx-cli as the user leaves it after setting it up by hand
        (tmp_path / "home" / ".wx-cli").mkdir(parents=True, exist_ok=True)
        (tmp_path / "home" / ".wx-cli" / "config.json").write_text("{}")
        cfg = Config(
            state_dir=str(tmp_path / "state"),
            wx_home=str(tmp_path / "home"),
            access_token="secret",
            reverse_url="",
            self_wxid=cfg_kwargs.pop("self_wxid", "wxid_self"),
            self_nickname="洛洛",
            image_extract=False,
            **{"max_backlog_seconds": 10**10, **cfg_kwargs},
        )
        cfg.ui = merge_ui(DEFAULT_UI, ui_overrides or {})
        ui = UiDriver(cfg.ui, fake_x, sleep=no_sleep)
        state = State()
        state.wechat_pid = 1
        state.login = LOGIN_OK
        gw = Gateway(cfg, store=Store(), wx=WxCli("wx", runner=fake_wx), ui=ui, state=state, proc_dir=str(tmp_path / "proc"))
        gw._first_poll = False
        return gw

    return make
