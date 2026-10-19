"""自我叙事：她自己的生活线。

设计意图：她不是只在被说话时才存在。每天有一份「今天大概会怎么过」的日程，
白天由心跳（Keeper）推进一些零散小事，这些小事只写进 life_log，**不主动发消息**；
聊天时把它们注入上下文，她就会自然地带出来（"今天稿子被退了两回"）。

所有生成都走小 prompt，控制成本。
"""

from __future__ import annotations

import json
import logging
import random
import time
from datetime import date, datetime, timedelta
from typing import Any

from .brain import Brain, BrainError
from .config import settings
from .emotion import Mood
from .store import Store

log = logging.getLogger("qqbot.life")


def _p() -> str:
    from . import personas

    return personas.active()

LIFE_SYSTEM = """你在扮演「沈知微」的日常，为她生成符合人设的生活碎片。只输出 JSON，不要多余文字。
她：27 岁，杭州，文学图书编辑，独居，猫叫芝麻，说话温柔知性但不端，偶尔自嘲。"""

PLAN_PROMPT = """今天是 {day}（{weekday}）。她现在的心情：{mood}。

请生成她今天的安排，输出 JSON：
{{"mood_start":"早上的一句话心情","plan":["两三件今天要做的事，具体到动作"],"intention":"今天心里挂着的一个念头"}}

要求：3 条 plan 足够；贴近真实编辑的工作与杭州的普通一天；不要写"与用户互动"，她不知道今天会发生什么。"""

EVENT_PROMPT = """今天是 {day}，现在 {time}。她今天的心情：{mood}。

今天的安排：
{plan}

已经发生的事：
{recent}

请生成此刻的一件小事（她一个人经历或想到的），输出 JSON：
{{"text":"一两句具体的细节，有画面感","tag":"work|life|thought|mood","emotion":"这件事之后的心情一个词"}}

要求：承接上面的安排推进，像是同一段时间线里自然长出来的；不要每次都跟工作有关；不要提到"用户"。"""


class LifeService:
    def __init__(self, store: Store, brain: Brain, mood: Mood) -> None:
        self.store = store
        self.brain = brain
        self.mood = mood
        self.stats: dict[str, Any] = {
            "plans": 0,
            "events": 0,
            "skipped": 0,
            "errors": 0,
            "last_event_at": None,
            "last_error": None,
        }

    @staticmethod
    def today() -> str:
        return date.today().strftime("%Y-%m-%d")

    def plan_of(self, day: str | None = None) -> dict[str, Any]:
        day = day or self.today()
        row = self.store.latest_life_of_kind(day, "plan", persona=_p())
        if not row:
            return {}
        try:
            data = json.loads(row.get("content") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {"plan": [row.get("content")]}

    async def ensure_plan(self, day: str | None = None) -> dict[str, Any]:
        day = day or self.today()
        existing = self.plan_of(day)
        if existing:
            return existing
        if not self.brain.ready:
            return {}
        weekday = "一二三四五六日"[(datetime.now().weekday())]
        payload = PLAN_PROMPT.format(day=day, weekday=weekday, mood=self.mood.label())
        try:
            out = await self.brain.complete(
                [
                    {"role": "system", "content": LIFE_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.9,
                max_tokens=320,
            )
        except BrainError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            log.warning("生成日程失败：%s", exc)
            return {}
        data = _extract_json(out.get("text") or "")
        if not data:
            return {}
        self.store.add_life(
            json.dumps(data, ensure_ascii=False),
            day=day,
            kind="plan",
            mood=str(data.get("mood_start") or "")[:60] or None,
            tags=["plan"],
        persona=_p(),
    )
        self.stats["plans"] += 1
        log.info("生成今日日程 day=%s plan=%s", day, "；".join(map(str, data.get("plan") or []))[:120])
        return data

    async def tick(self, *, force: bool = False) -> dict[str, Any]:
        """心跳每拍调用：按概率推进一件小事。深夜不生成。"""
        if not settings.life_enabled and not force:
            return {"skipped": "disabled"}
        now = datetime.now()
        if not force and (now.hour >= 23 or now.hour < 7):
            self.stats["skipped"] += 1
            return {"skipped": "quiet hours"}
        day = self.today()
        plan = await self.ensure_plan(day)
        if not plan:
            return {"skipped": "no plan"}
        probability = settings.life_event_probability
        if probability < 1.0 and not force and random.random() > probability:
            self.stats["skipped"] += 1
            return {"skipped": "dice"}
        recent_rows = self.store.life_log(kind="event", day=day, limit=3, persona=_p())
        recent = "\n".join(f"- {row.get('content')}" for row in recent_rows) or "（还没有）"
        payload = EVENT_PROMPT.format(
            day=day,
            time=now.strftime("%H:%M"),
            mood=self.mood.label(),
            plan="\n".join(f"- {item}" for item in plan.get("plan") or []),
            recent=recent,
        )
        try:
            out = await self.brain.complete(
                [
                    {"role": "system", "content": LIFE_SYSTEM},
                    {"role": "user", "content": payload},
                ],
                temperature=0.95,
                max_tokens=200,
            )
        except BrainError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            log.warning("生成生活事件失败：%s", exc)
            return {"error": str(exc)}
        data = _extract_json(out.get("text") or "")
        text = " ".join(str(data.get("text") or "").split()).strip()
        if len(text) < 4:
            self.stats["skipped"] += 1
            return {"skipped": "empty"}
        event_id = self.store.add_life(
            text,
            day=day,
            kind="event",
            mood=str(data.get("emotion") or "")[:32] or None,
            tags=[str(data.get("tag") or "life")[:24]],
        persona=_p(),
    )
        self.stats["events"] += 1
        self.stats["last_event_at"] = time.time()
        log.info("生活线 +1 day=%s id=%s text=%r", day, event_id, text[:80])
        return {"id": event_id, "text": text, "emotion": data.get("emotion")}

    def context_block(self, day: str | None = None, *, limit: int = 3) -> str:
        day = day or self.today()
        plan = self.plan_of(day)
        rows = self.store.life_log(kind="event", day=day, limit=limit, persona=_p())
        yesterday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        prev = self.store.get_day_summary(yesterday, persona=_p())
        lines: list[str] = []
        if plan.get("mood_start"):
            lines.append(f"今天醒来时：{plan['mood_start']}")
        if plan.get("plan"):
            lines.append("今天的安排：" + "；".join(str(item) for item in plan.get("plan") or []))
        if plan.get("intention"):
            lines.append(f"心里挂着：{plan['intention']}")
        if rows:
            lines.append("今天已经发生：")
            for row in rows:
                when = datetime.fromtimestamp(int(row.get("at") or 0)).strftime("%H:%M")
                lines.append(f"- {when} {row.get('content')}")
        if prev:
            lines.append(f"昨天（{yesterday}）：{prev.get('summary')}")
        if not lines:
            return ""
        return "你自己这一两天（聊天时自然带出来，别整段念）：\n" + "\n".join(lines)

    def status(self) -> dict[str, Any]:
        day = self.today()
        return {
            "today": day,
            "plan": self.plan_of(day),
            "events": self.store.life_log(kind="event", day=day, limit=10, persona=_p()),
            "stats": self.stats,
        }


def _extract_json(text: str) -> dict[str, Any]:
    start = str(text or "").find("{")
    end = str(text or "").rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}
