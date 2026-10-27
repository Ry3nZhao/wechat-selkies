"""Drive the WeChat window with xdotool and xclip.

Each step is a small method that issues a handful of X commands through an injectable
runner, so tests can check the exact command sequence without an X server. Keys, delays
and quote coordinates come from the UI config (see ``config.DEFAULT_UI``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from .status import LOGIN_NO_WINDOW, LOGIN_OK, LOGIN_SCREEN

log = logging.getLogger(__name__)

# (argv, stdin text or None, timeout, capture output) -> (returncode, stdout, stderr)
XRunner = Callable[[Sequence[str], str | None, float, bool], Awaitable[tuple[int, str, str]]]
Sleep = Callable[[float], Awaitable[None]]


class UiError(Exception):
    pass


@dataclass
class Window:
    wid: str
    title: str
    x: int
    y: int
    width: int
    height: int

    def point(self, frac: Sequence[float]) -> tuple[int, int]:
        """Absolute screen point for a (fx, fy) fraction of the window."""
        return (self.x + int(self.width * float(frac[0])), self.y + int(self.height * float(frac[1])))


def make_x_runner(display: str) -> XRunner:
    env = dict(os.environ)
    env["DISPLAY"] = display

    async def run(argv: Sequence[str], stdin: str | None, timeout: float, capture: bool) -> tuple[int, str, str]:
        pipe = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=pipe,
                stderr=pipe,
                env=env,
            )
        except FileNotFoundError:
            return 127, "", f"{argv[0]} not found"
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(stdin.encode("utf-8") if stdin is not None else None), timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"{argv[0]} timed out"
        return proc.returncode or 0, (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace")

    return run


def parse_geometry(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if value.strip().lstrip("-").isdigit():
            out[key.strip()] = int(value)
    return out


class UiDriver:
    def __init__(
        self,
        ui: dict[str, Any],
        runner: XRunner,
        *,
        sleep: Sleep = asyncio.sleep,
        screenshot_dir: Path | None = None,
        grab: Callable[[], Any] | None = None,
    ) -> None:
        self.ui = ui
        self._run = runner
        self._sleep = sleep
        self.screenshot_dir = screenshot_dir
        self._grab = grab

    # ---- primitives --------------------------------------------------------------------

    async def x(self, *argv: str, stdin: str | None = None, capture: bool = True, check: bool = True) -> str:
        code, out, err = await self._run(list(argv), stdin, 10.0, capture)
        if check and code != 0:
            raise UiError(f"{' '.join(argv[:2])} failed ({code}): {err.strip()[:200]}")
        return out

    async def pause(self, key: str = "step_delay") -> None:
        await self._sleep(float(self.ui.get(key, 0.3)))

    async def key(self, keys: str) -> None:
        await self.x("xdotool", "key", "--clearmodifiers", keys, capture=False)

    async def type_text(self, text: str) -> None:
        await self.x("xdotool", "type", "--clearmodifiers", "--delay", "40", "--", text, capture=False)

    async def set_clipboard(self, text: str) -> None:
        # xclip keeps serving the selection in the background, so its output is not captured.
        await self.x("xclip", "-selection", "clipboard", "-i", stdin=text, capture=False)

    async def paste(self, text: str) -> None:
        await self.set_clipboard(text)
        await self.pause()
        await self.key(self.ui["paste_key"])
        await self.pause()

    # ---- windows -----------------------------------------------------------------------

    async def windows(self) -> list[Window]:
        code, out, _ = await self._run(
            ["xdotool", "search", "--onlyvisible", "--class", self.ui["window_class"]], None, 10.0, True
        )
        if code != 0:
            return []
        found = []
        for wid in out.split():
            _, title, _ = await self._run(["xdotool", "getwindowname", wid], None, 5.0, True)
            _, geom, _ = await self._run(["xdotool", "getwindowgeometry", "--shell", wid], None, 5.0, True)
            g = parse_geometry(geom)
            found.append(Window(wid, title.strip(), g.get("X", 0), g.get("Y", 0), g.get("WIDTH", 0), g.get("HEIGHT", 0)))
        return found

    async def detect_login(self) -> tuple[str, str]:
        wins = await self.windows()
        min_w, min_h = self.ui.get("min_main_window", [600, 400])
        titles = set(self.ui.get("logged_in_titles", []))
        big = [w for w in wins if w.width >= 300 and w.height >= 300]
        if not big:
            return LOGIN_NO_WINDOW, ""
        for w in big:
            if w.title in titles and w.width >= min_w and w.height >= min_h:
                return LOGIN_OK, w.title
        return LOGIN_SCREEN, big[0].title

    async def main_window(self) -> Window:
        min_w, min_h = self.ui.get("min_main_window", [600, 400])
        titles = set(self.ui.get("logged_in_titles", []))
        for w in await self.windows():
            if w.title in titles and w.width >= min_w and w.height >= min_h:
                return w
        raise UiError("找不到已登录的微信主窗口")

    async def activate(self, win: Window) -> None:
        await self.x("xdotool", "windowactivate", "--sync", win.wid, capture=False)
        await self.pause()

    # ---- steps -------------------------------------------------------------------------

    async def open_chat(self, name: str) -> None:
        """Search the conversation by its display name and open the first hit."""
        await self.key(self.ui["search_key"])
        await self.pause()
        await self.paste(name)
        await self.pause("search_delay")
        await self.key("Return")
        await self.pause("search_delay")

    async def insert_mention(self, prefix: str) -> None:
        """Type "@", filter the member picker with ``prefix`` and confirm the first hit."""
        await self.type_text("@")
        await self.pause("at_popup_delay")
        if self.ui.get("at_input") == "type":
            await self.type_text(prefix)
        else:
            await self.paste(prefix)
        await self.pause("at_popup_delay")
        await self.key(self.ui["at_confirm_key"])
        await self.pause()

    async def quote_last(self, win: Window, from_self: bool) -> None:
        """Right-click the newest bubble and choose the quote entry from its menu."""
        q = self.ui["quote"]
        x, y = win.point(q["self_last_point"] if from_self else q["other_last_point"])
        await self.x("xdotool", "mousemove", str(x), str(y), capture=False)
        await self.pause()
        await self.x("xdotool", "click", "3", capture=False)
        await self._sleep(float(q.get("menu_delay", 0.5)))
        if q.get("menu_offset"):
            dx, dy = q["menu_offset"]
            await self.x("xdotool", "mousemove", str(x + int(dx)), str(y + int(dy)), capture=False)
            await self.pause()
            await self.x("xdotool", "click", "1", capture=False)
        elif q.get("menu_keys"):
            for k in q["menu_keys"]:
                await self.key(k)
                await self._sleep(0.1)
        else:
            await self.key(self.ui["escape_key"])
            raise UiError("引用菜单未配置（quote.menu_offset / quote.menu_keys）")
        await self.pause()

    async def send(self) -> None:
        await self.key(self.ui["send_key"])
        await self.pause()

    async def cancel(self) -> None:
        await self.x("xdotool", "key", "--clearmodifiers", self.ui["escape_key"], capture=False, check=False)

    async def screenshot(self, label: str) -> str | None:
        """Save a screenshot when debug screenshots are on; returns the path."""
        if not self.ui.get("debug_screenshots") or self.screenshot_dir is None:
            return None
        try:
            grab = self._grab
            if grab is None:
                from PIL import ImageGrab  # imported lazily; needs an X display

                grab = ImageGrab.grab
            image = await asyncio.to_thread(grab)
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            path = self.screenshot_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}-{label}.png"
            await asyncio.to_thread(image.save, path)
            return str(path)
        except Exception as exc:  # debugging aid only; never break a send
            log.warning("screenshot %s failed: %s", label, exc)
            return None
