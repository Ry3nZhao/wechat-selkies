"""Runtime configuration from environment variables plus an optional UI calibration file."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger(__name__)

# Defaults for driving the WeChat window. Every value can be overridden in the UI config
# file (JSON, merged one level deep), so a new WeChat build can be recalibrated without a
# code change. Points are fractions of the main window's width/height.
DEFAULT_UI: dict[str, Any] = {
    "window_class": "wechat",
    # Titles of the main window once logged in; anything else visible is treated as the
    # login screen.
    "logged_in_titles": ["Weixin", "微信"],
    "min_main_window": [600, 400],
    "search_key": "ctrl+f",
    "paste_key": "ctrl+v",
    "send_key": "Return",
    "escape_key": "Escape",
    "step_delay": 0.35,
    "search_delay": 1.2,
    # Native @: type "@", wait for the member picker, enter a nickname prefix, confirm.
    "at_popup_delay": 0.7,
    "at_input": "paste",  # "paste" (clipboard, CJK safe) or "type" (xdotool type)
    "at_confirm_key": "Return",
    "at_prefix_max_len": 8,
    # Quote: right-click the target bubble and pick the menu entry. Disabled until the
    # coordinates are calibrated on the real client: a wrong menu entry could do something
    # else (forward, delete), so the default degrades to a plain-text quote.
    "quote": {
        "enabled": False,
        "other_last_point": [0.42, 0.78],
        "self_last_point": [0.92, 0.78],
        # Either a key sequence that walks the context menu to the quote entry ...
        "menu_keys": [],
        # ... or a pixel offset from the right-click point to the quote entry.
        "menu_offset": None,
        "menu_delay": 0.5,
    },
    "debug_screenshots": False,
}


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    value = env.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    value = env.get(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        log.warning("ignoring invalid %s=%r", name, value)
        return default


def merge_ui(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8095
    access_token: str = ""
    reverse_url: str = "ws://astrbot:6199/ws"
    reverse_token: str = ""
    reverse_max_backoff: float = 60.0
    poll_interval: float = 1.5
    recheck_interval: float = 30.0
    status_interval: float = 5.0
    heartbeat_interval: float = 30.0
    max_backlog_seconds: float = 300.0
    wx_bin: str = "/usr/local/bin/wx"
    wx_timeout: float = 20.0
    wx_home: str = "/config"
    state_dir: str = "/config/wechat-api"
    self_wxid: str = ""
    self_nickname: str = ""
    display: str = ":1"
    wechat_process_patterns: tuple[str, ...] = ("/opt/wechat/wechat", "/usr/bin/wechat")
    image_extract: bool = True
    send_verify_timeout: float = 15.0
    send_wait_verify: bool = False
    ui: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_UI)))

    @property
    def screenshot_dir(self) -> Path:
        return Path(self.state_dir) / "screenshots"

    @property
    def image_dir(self) -> Path:
        return Path(self.state_dir) / "images"

    @property
    def db_path(self) -> Path:
        return Path(self.state_dir) / "wechat-api.sqlite3"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        token = env.get("WECHAT_API_TOKEN", "")
        cfg = cls(
            host=env.get("WECHAT_API_HOST", "127.0.0.1"),
            port=int(env.get("WECHAT_API_PORT", "8095")),
            access_token=token,
            reverse_url=env.get("ONEBOT_REVERSE_URL", "ws://astrbot:6199/ws"),
            reverse_token=env.get("ONEBOT_REVERSE_TOKEN", token),
            reverse_max_backoff=_env_float(env, "ONEBOT_REVERSE_MAX_BACKOFF", 60.0),
            poll_interval=_env_float(env, "WECHAT_API_POLL_INTERVAL", 1.5),
            recheck_interval=_env_float(env, "WECHAT_API_RECHECK_INTERVAL", 30.0),
            heartbeat_interval=_env_float(env, "WECHAT_API_HEARTBEAT_INTERVAL", 30.0),
            max_backlog_seconds=_env_float(env, "WECHAT_API_MAX_BACKLOG_SECONDS", 300.0),
            wx_bin=env.get("WX_BIN", "/usr/local/bin/wx"),
            wx_timeout=_env_float(env, "WX_TIMEOUT", 20.0),
            wx_home=env.get("WX_HOME", env.get("HOME", "/config")),
            state_dir=env.get("WECHAT_API_STATE_DIR", "/config/wechat-api"),
            self_wxid=env.get("WECHAT_SELF_WXID", ""),
            self_nickname=env.get("WECHAT_SELF_NICKNAME", ""),
            display=env.get("DISPLAY", ":1") or ":1",
            image_extract=_env_bool(env, "WECHAT_API_IMAGE_EXTRACT", True),
            send_verify_timeout=_env_float(env, "WECHAT_API_SEND_VERIFY_TIMEOUT", 15.0),
            send_wait_verify=_env_bool(env, "WECHAT_API_SEND_WAIT_VERIFY", False),
        )
        ui_path = Path(env.get("WECHAT_API_UI_CONFIG", str(Path(cfg.state_dir) / "ui.json")))
        if ui_path.is_file():
            try:
                cfg.ui = merge_ui(DEFAULT_UI, json.loads(ui_path.read_text("utf-8")))
            except (OSError, ValueError) as exc:
                log.error("cannot read UI config %s: %s", ui_path, exc)
        if _env_bool(env, "WECHAT_API_DEBUG_SCREENSHOTS", False):
            cfg.ui["debug_screenshots"] = True
        return cfg
