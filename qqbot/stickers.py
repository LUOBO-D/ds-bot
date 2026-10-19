"""她的表情包库。

好友发来表情包时，她按自己的喜好决定要不要收下来（存库 + 打分 + 标签），
以后聊天时偶尔挑一个发出去，像真人互相斗图。

数据在 `stickers` 表；图片本体沿用 `data/inbox/` 里已经落盘的文件。
"""

from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path
from typing import Any

from . import personas
from .brain import Brain, BrainError
from .config import settings
from .store import Store
from .tools import Actions

log = logging.getLogger("qqbot.stickers")

JUDGE_PROMPT = """好友发来一个表情包，画面是：{desc}
你此刻的心情：{mood}
你们刚才在聊：{context}

站在你自己的角度：这个表情包你喜不喜欢？以后聊天时会不会想发出去？

第一行只写「存」或「不存」；
第二行写 2~4 个标签，用逗号分隔（比如：无语,嫌弃,可爱）；
第三行写一句 12 字以内的备注（你自己会怎么用它）。"""


def _parse_judgement(text: str) -> tuple[bool, str, str]:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    keep = False
    tags = ""
    note = ""
    if lines:
        head = lines[0].strip("。 .！!「」\"'")
        keep = head.startswith("存") or head.endswith("存") and "不" not in head[:3]
        if "不存" in head[:4] or head.startswith("不"):
            keep = False
    if len(lines) > 1:
        tags = re.sub(r"\s+", "", lines[1]).strip("，,、")
        tags = ",".join(part for part in re.split(r"[,，、/]+", tags) if part)[:40]
    if len(lines) > 2:
        note = lines[2].strip("「」\"'")[:24]
    return keep, tags, note


class StickerBook:
    def __init__(
        self,
        store: Store,
        brain: Brain,
        actions: Actions,
        *,
        vision: Any | None = None,
        mood: Any | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.store = store
        self.brain = brain
        self.actions = actions
        self.vision = vision
        self.mood = mood
        self.rng = rng or random.Random()
        self.stats: dict[str, Any] = {
            "judged": 0,
            "saved": 0,
            "skipped": 0,
            "sent": 0,
            "send_skipped": 0,
            "errors": 0,
        }

    # ---------- 基础 ----------
    @property
    def enabled(self) -> bool:
        return bool(settings.sticker_enabled) and settings.sticker_max > 0

    def _mood_text(self) -> str:
        if self.mood is None:
            return "还行"
        try:
            return str(self.mood.label())
        except Exception:  # noqa: BLE001
            return "还行"

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "stats": dict(self.stats),
            "library": self.store.sticker_stats(),
            "send_probability": settings.sticker_send_probability,
            "daily_max": settings.sticker_send_daily_max,
            "cooldown_seconds": settings.sticker_send_cooldown,
        }

    # ---------- 收下 / 挑表情包 ----------
    async def judge(self, desc: str, *, context: str = "") -> tuple[bool, str, str]:
        prompt = JUDGE_PROMPT.format(
            desc=(desc or "（没看清）")[:120],
            mood=self._mood_text(),
            context=(context or "随便聊着")[:120],
        )
        out = await self.brain.complete(
            [
                {"role": "system", "content": "你在看好友发来的表情包，按自己的喜好决定收不收。只按格式回答，不要解释。"},
                {"role": "user", "content": prompt},
            ],
            temperature=settings.sticker_judge_temperature,
            max_tokens=90,
        )
        keep, tags, note = _parse_judgement(out.get("text") or "")
        return keep, tags, note

    async def consider(
        self,
        *,
        url: str = "",
        path: str | None = None,
        desc: str = "",
        sender_id: int | None = None,
        message_id: int | None = None,
        context: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        """好友发来表情包后调用：识图 → 自己判断喜不喜欢 → 喜欢就入库。"""
        if not self.enabled:
            return {"ok": False, "reason": "表情包库未启用"}
        if self.vision is None:
            return {"ok": False, "reason": "没有识图能力"}
        target: Path | None = None
        digest = ""
        summary = (desc or "").strip()
        if path:
            target = Path(path)
        elif url:
            try:
                got = await self.vision.download(url)
            except Exception:  # noqa: BLE001
                log.exception("表情包下载失败")
                got = None
            if got:
                target, digest = got
        if target is None or not target.exists():
            return {"ok": False, "reason": "图片没拿到"}
        if digest:
            cached = self.store.image_by_digest(digest)
            if cached and cached.get("summary"):
                summary = str(cached["summary"])
        if not summary:
            try:
                summary = await self.vision.describe_file(target, sticker=True) or ""
            except Exception:  # noqa: BLE001
                log.exception("表情包识图失败")
        if not summary:
            return {"ok": False, "reason": "没看清表情包"}
        if not digest:
            digest = _digest_of(target)
        existing = self.store.sticker_by_digest(digest)
        if existing:
            return {"ok": True, "saved": False, "reason": "已经收过", "sticker": existing}
        try:
            keep, tags, note = await self.judge(summary, context=context)
        except BrainError as exc:
            log.warning("表情包判断失败: %s", exc)
            await self._stats_inc("errors")
            return {"ok": False, "reason": f"判断失败: {exc}"}
        self.stats["judged"] += 1
        if not keep:
            self.stats["skipped"] += 1
            log.info("表情包不收 summary=%r", summary[:40])
            return {"ok": True, "saved": False, "summary": summary}
        if len(self.store.stickers(limit=settings.sticker_max + 1)) >= settings.sticker_max:
            oldest = self.store.stickers(limit=settings.sticker_max + 1)[-1]
            self.store.delete_sticker(int(oldest["id"]))
        rating = round(self.rng.uniform(0.6, 1.0), 2)
        sticker_id = self.store.add_sticker(
            digest=digest,
            path=str(target),
            summary=summary,
            tags=tags,
            note=note,
            rating=rating,
            sender_id=sender_id,
            message_id=message_id,
            persona=personas.active(),
        )
        self.stats["saved"] += 1
        log.info("收藏表情包 id=%s rating=%.2f tags=%r summary=%r", sticker_id, rating, tags, summary[:40])
        return {
            "ok": True,
            "saved": True,
            "id": sticker_id,
            "rating": rating,
            "tags": tags,
            "note": note,
            "summary": summary,
        }

    async def _stats_inc(self, key: str) -> None:
        self.stats[key] = int(self.stats.get(key) or 0) + 1

    def pick(self, *, exclude: str | None = None) -> dict[str, Any] | None:
        items = [item for item in self.store.stickers(limit=settings.sticker_max) if item.get("path")]
        if exclude:
            items = [item for item in items if item.get("digest") != exclude]
        if not items:
            return None
        weights = [max(0.05, float(item.get("rating") or 0.5)) for item in items]
        return self.rng.choices(items, weights=weights, k=1)[0]

    def context_block(self, limit: int = 5) -> str:
        items = [item for item in self.store.stickers(limit=limit) if item.get("summary")]
        if not items:
            return ""
        lines = []
        for item in items:
            tags = f"（{item['tags']}）" if item.get("tags") else ""
            used = int(item.get("used_count") or 0)
            lines.append(f"- {item['summary']}{tags}" + ("（你发过）" if used else ""))
        return "你收藏的表情包（聊天时想发就发一张，别每次都用同一张）：\n" + "\n".join(lines)

    # ---------- 发出去 ----------
    async def maybe_send(
        self,
        uid: int,
        *,
        force: bool = False,
        chance: float | None = None,
        exclude: str | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        today = time.strftime("%Y-%m-%d")
        if self.store.get_kv("stickers.day") != today:
            self.store.set_kv("stickers.day", today)
            self.store.set_kv("stickers.sent", "0")
        sent_today = int(self.store.get_kv("stickers.sent") or 0)
        if sent_today >= settings.sticker_send_daily_max:
            self.stats["send_skipped"] += 1
            return None
        last = float(self.store.get_kv("stickers.last_sent_at") or 0)
        if not force and time.time() - last < settings.sticker_send_cooldown:
            self.stats["send_skipped"] += 1
            return None
        probability = settings.sticker_send_probability if chance is None else chance
        if not force and self.rng.random() > probability:
            self.stats["send_skipped"] += 1
            return None
        sticker = self.pick(exclude=exclude)
        if sticker is None:
            return None
        try:
            await self.actions.send_sticker("private", int(uid), file=str(sticker["path"]))
        except Exception:  # noqa: BLE001
            log.exception("发表情包失败 id=%s", sticker.get("id"))
            self.stats["errors"] += 1
            return None
        self.store.touch_sticker(int(sticker["id"]))
        self.store.set_kv("stickers.sent", str(sent_today + 1))
        self.store.set_kv("stickers.last_sent_at", str(int(time.time())))
        self.stats["sent"] += 1
        log.info(
            "发了个表情包 id=%s summary=%r to=%s",
            sticker.get("id"),
            str(sticker.get("summary") or "")[:30],
            uid,
        )
        return sticker


def _digest_of(path: Path) -> str:
    import hashlib

    h = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()
