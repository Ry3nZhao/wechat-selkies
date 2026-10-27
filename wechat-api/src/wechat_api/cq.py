"""OneBot v11 message helpers: CQ code strings <-> segment arrays."""

from __future__ import annotations

import re
from typing import Any

_CQ_RE = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^,\]]*)*)\]")


def escape(text: str, *, in_param: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    if in_param:
        text = text.replace(",", "&#44;")
    return text


def unescape(text: str) -> str:
    return (
        text.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")
    )


def parse_cq(message: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    pos = 0
    for match in _CQ_RE.finditer(message):
        if match.start() > pos:
            segments.append({"type": "text", "data": {"text": unescape(message[pos:match.start()])}})
        data: dict[str, Any] = {}
        for item in match.group(2).split(",")[1:]:
            key, _, value = item.partition("=")
            data[key] = unescape(value)
        segments.append({"type": match.group(1), "data": data})
        pos = match.end()
    if pos < len(message):
        segments.append({"type": "text", "data": {"text": unescape(message[pos:])}})
    return segments


def normalize_message(message: Any, auto_escape: bool = False) -> list[dict[str, Any]]:
    """Accept a CQ string, a single segment or a segment list; return a segment list."""
    if message is None:
        return []
    if isinstance(message, str):
        if auto_escape:
            return [{"type": "text", "data": {"text": message}}]
        return parse_cq(message)
    if isinstance(message, dict):
        message = [message]
    if not isinstance(message, list):
        raise ValueError("message must be a string or a segment list")
    out = []
    for seg in message:
        if not isinstance(seg, dict) or "type" not in seg:
            raise ValueError("invalid message segment")
        out.append({"type": str(seg["type"]), "data": dict(seg.get("data") or {})})
    return out


def to_cq(segments: list[dict[str, Any]]) -> str:
    parts = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(escape(str(seg["data"].get("text", ""))))
            continue
        params = "".join(
            f",{k}={escape(str(v), in_param=True)}"
            for k, v in seg["data"].items()
            if v is not None and not isinstance(v, (dict, list))
        )
        parts.append(f"[CQ:{seg['type']}{params}]")
    return "".join(parts)
