"""多角色卡：assets/personas/<slug>.md，当前生效的写在 active.json。

- 角色卡是纯 Markdown 正文，直接作为 system prompt 的 persona 段。
- 切换只改指针文件，下一轮请求立即生效（不需要重启）。
- 记忆 / 情绪 / 生活线都按角色 slug 隔离。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .config import settings

FALLBACK_PERSONA = "xiaojingyu"

# 卡里没写「名字：」时的兜底显示名
KNOWN_NAMES = {
    "shenzhiwei": "沈知微",
    "xiaojingyu": "小鲸鱼",
}

GENERIC_HEADINGS = {
    "你是谁",
    "人设",
    "设定",
    "角色",
    "角色卡",
    "说话方式",
    "说话铁律",
    "性格",
    "底线",
    "气质",
    "背景",
    "外貌",
    "关系",
}


def _dir() -> Path:
    path = settings.personas_dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pointer() -> Path:
    return _dir() / "active.json"


def slugs() -> list[str]:
    return sorted(path.stem for path in _dir().glob("*.md"))


def path_of(slug: str) -> Path:
    return _dir() / f"{slug}.md"


def exists(slug: str | None) -> bool:
    return bool(slug) and path_of(str(slug)).exists()


def active() -> str:
    try:
        data = json.loads(_pointer().read_text(encoding="utf-8"))
        slug = str(data.get("active") or "").strip()
        if exists(slug):
            return slug
    except FileNotFoundError:
        pass
    except Exception:
        pass
    configured = (settings.persona_default or "").strip()
    if exists(configured):
        return configured
    if exists(FALLBACK_PERSONA):
        return FALLBACK_PERSONA
    candidates = slugs()
    return candidates[0] if candidates else ""


def set_active(slug: str) -> str:
    slug = (slug or "").strip()
    if not exists(slug):
        raise KeyError(slug)
    _pointer().write_text(
        json.dumps({"active": slug, "at": int(time.time())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return slug


def text(slug: str | None = None) -> str:
    slug = slug or active()
    if exists(slug):
        try:
            return path_of(str(slug)).read_text(encoding="utf-8")
        except Exception:
            return ""
    return ""


def name(slug: str | None = None) -> str:
    slug = slug or active()
    body = text(slug)
    for pattern in (
        r"名字[:：]\s*([^\n，。；、]+)",
        r"^#+\s*角色卡[:：]\s*([^\n]+)",
        r"^#+\s*([^\n]+)",
    ):
        match = re.search(pattern, body, re.M)
        if not match:
            continue
        value = match.group(1).strip().strip("*`「」\"'“”")
        if pattern.startswith(r"^#+\s*([^\n]") and value in GENERIC_HEADINGS:
            continue
        if value:
            return value
    return KNOWN_NAMES.get(str(slug or ""), str(slug or ""))


def resolve(query: str | None) -> str | None:
    """把用户说的名字 / 简称 / slug 对上某张卡。"""
    raw = (query or "").strip().strip("「」\"'“”").lower()
    if not raw:
        return None
    if exists(raw):
        return raw
    for slug in slugs():
        if raw == slug.lower():
            return slug
    for slug in slugs():
        card_name = name(slug).lower()
        if card_name and (raw in card_name or card_name in raw):
            return slug
    for slug in slugs():
        if raw in text(slug).lower():
            return slug
    return None


def listing() -> list[dict[str, Any]]:
    current = active()
    out: list[dict[str, Any]] = []
    for slug in slugs():
        body = text(slug)
        lines = [line.strip() for line in body.splitlines() if line.strip() and not line.startswith("#")]
        out.append(
            {
                "slug": slug,
                "name": name(slug),
                "active": slug == current,
                "summary": (lines[0] if lines else "")[:100],
            }
        )
    return out
