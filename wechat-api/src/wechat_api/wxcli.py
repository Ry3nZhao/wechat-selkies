"""Thin async wrapper around wx-cli-again's read-only commands.

The gateway never prepares wx-cli itself: the user sets wx-cli up by hand inside the
container, following wx-cli-again's own documentation. This wrapper only runs the read
commands listed in ``READ_ONLY_COMMANDS``; anything else is refused.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

READ_ONLY_COMMANDS = frozenset(
    {"new-messages", "members", "sessions", "doctor", "attachments", "extract", "--version"}
)

# Doctor checks whose failure means wx-cli has not been set up (or its setup is stale) and
# the user has to redo it by hand. Other checks (macOS-only items and the like) are ignored.
SETUP_CHECKS = frozenset({"config.json", "数据库密钥", "关键分片密钥", "SQLCipher 在线打开"})

# A runner takes argv and returns (returncode, stdout, stderr). Tests inject a fake one.
Runner = Callable[[Sequence[str], float], Awaitable[tuple[int, str, str]]]


class WxError(Exception):
    """A wx-cli invocation failed (non-zero exit, timeout, bad JSON, missing binary)."""

    def __init__(self, message: str, *, missing_binary: bool = False) -> None:
        super().__init__(message)
        self.missing_binary = missing_binary


@dataclass
class DoctorResult:
    ok: bool
    needs_user_init: bool
    failed: list[str] = field(default_factory=list)
    detail: str = ""


async def subprocess_runner(argv: Sequence[str], timeout: float, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError as exc:
        raise WxError(f"{argv[0]} not found", missing_binary=True) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise WxError(f"{' '.join(argv[:2])} timed out after {timeout:.0f}s") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class WxCli:
    def __init__(self, binary: str, *, timeout: float = 20.0, home: str | None = None, runner: Runner | None = None) -> None:
        self.binary = binary
        self.timeout = timeout
        if runner is None:
            env = dict(os.environ)
            if home:
                env["HOME"] = home

            async def runner(argv: Sequence[str], t: float) -> tuple[int, str, str]:
                return await subprocess_runner(argv, t, env)

        self._runner = runner

    async def _run(self, *args: str, timeout: float | None = None) -> str:
        if not args or args[0] not in READ_ONLY_COMMANDS:
            raise ValueError(f"wx {args[:1]} is not a read-only command")
        code, out, err = await self._runner([self.binary, *args], timeout or self.timeout)
        if code != 0:
            raise WxError((err or out).strip()[-500:] or f"wx {args[0]} exited with {code}")
        return out

    async def _json(self, *args: str, timeout: float | None = None) -> Any:
        out = await self._run(*args, timeout=timeout)
        try:
            return json.loads(out)
        except ValueError as exc:
            raise WxError(f"wx {args[0]}: invalid JSON output") from exc

    async def version(self) -> str:
        return (await self._run("--version", timeout=10)).strip()

    async def new_messages(self, limit: int = 200) -> dict[str, Any]:
        data = await self._json("new-messages", "--json", "-n", str(limit))
        if not isinstance(data, dict):
            raise WxError("wx new-messages: unexpected output")
        return data

    async def members(self, chat: str) -> list[dict[str, Any]]:
        data = await self._json("members", chat, "--json")
        if isinstance(data, dict):
            data = data.get("members", [])
        return [m for m in data if isinstance(m, dict)]

    async def sessions(self, limit: int = 200) -> list[dict[str, Any]]:
        data = await self._json("sessions", "--json", "-n", str(limit))
        items = data.get("sessions", []) if isinstance(data, dict) else data
        return [s for s in items if isinstance(s, dict)]

    async def attachments(self, chat: str, limit: int = 30) -> list[dict[str, Any]]:
        data = await self._json("attachments", chat, "--kind", "image", "-n", str(limit), "--json")
        items = data.get("attachments", []) if isinstance(data, dict) else data
        return [a for a in items if isinstance(a, dict)]

    async def extract(self, attachment_id: str, output: str) -> dict[str, Any]:
        data = await self._json("extract", attachment_id, "-o", output, "--overwrite", "--json")
        return data if isinstance(data, dict) else {}

    async def doctor(self) -> DoctorResult:
        try:
            data = await self._json("doctor", "--json", timeout=30)
        except WxError as exc:
            if exc.missing_binary:
                return DoctorResult(False, False, [], f"wx-cli 不可用：{exc}")
            return DoctorResult(False, False, [], f"wx doctor 失败：{exc}")
        return evaluate_doctor(data)


def evaluate_doctor(data: Any) -> DoctorResult:
    checks = data.get("checks", []) if isinstance(data, dict) else []
    failed = [
        str(c.get("name"))
        for c in checks
        if isinstance(c, dict) and not c.get("ok") and c.get("name") in SETUP_CHECKS
    ]
    if failed:
        return DoctorResult(
            False,
            True,
            failed,
            "需要用户初始化 wx-cli（未通过：" + "、".join(failed) + "）",
        )
    if not checks:
        return DoctorResult(False, False, [], "wx doctor 没有返回检查项")
    return DoctorResult(True, False, [], "wx-cli 读取检查通过")
