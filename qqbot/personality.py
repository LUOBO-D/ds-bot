"""性格层：五个维度的「基线 + 当前状态」+ 年轮留痕。

和情绪的区别：
- 情绪（emotion.py）是分钟~小时的起伏，会回落；
- 性格是周~月的倾向，日常波动会朝基线回，只有分量重的事件才会
  真正推动基线（年轮），而且有上限、可回滚。

五个维度（0~100，出厂值见 FACTORY）：
- closeness 亲密感：愿不愿意靠近、主不主动
- talkativeness 话量：爱不爱说、说多说少
- sensitivity 敏感度：对语气和细节的在意程度
- tsundere 傲娇：嘴硬程度
- stability 稳定性：情绪起伏与恢复速度

对外：snapshot()（接口）、prompt_block()（给大脑看）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from .config import settings
from . import personas
from .store import Store

log = logging.getLogger("qqbot.personality")

DIMS: dict[str, str] = {
    "closeness": "亲密感",
    "talkativeness": "话量",
    "sensitivity": "敏感度",
    "tsundere": "傲娇",
    "stability": "稳定性",
}

FACTORY: dict[str, float] = {
    "closeness": 50.0,
    "talkativeness": 55.0,
    "sensitivity": 45.0,
    "tsundere": 60.0,
    "stability": 65.0,
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _kv_key() -> str:
    return f"traits:{personas.active() or 'default'}"


class Personality:
    def __init__(self, store: Store, brain: Any = None) -> None:
        self.store = store
        self.brain = brain
        self.stats: dict[str, Any] = {"nudges": 0, "marks": 0, "settles": 0, "narratives": 0, "last_error": None}
        self._state = self._load()

    # ------------------------------------------------------------ 存取
    def _defaults(self) -> dict[str, Any]:
        return {
            "base": dict(FACTORY),
            "now": dict(FACTORY),
            "updated_at": int(time.time()),
            "last_settle": 0.0,
            "narrative": "",
            "narrative_at": 0,
        }

    def _load(self) -> dict[str, Any]:
        raw = self.store.get_kv(_kv_key())
        state = self._defaults()
        if isinstance(raw, dict):
            base = raw.get("base") if isinstance(raw.get("base"), dict) else {}
            now = raw.get("now") if isinstance(raw.get("now"), dict) else {}
            for key in DIMS:
                try:
                    state["base"][key] = _clamp(base.get(key, FACTORY[key]))
                    state["now"][key] = _clamp(now.get(key, state["base"][key]))
                except (TypeError, ValueError):
                    pass
            state["updated_at"] = raw.get("updated_at") or state["updated_at"]
            state["last_settle"] = float(raw.get("last_settle") or 0)
            state["narrative"] = str(raw.get("narrative") or "")
            state["narrative_at"] = raw.get("narrative_at") or 0
        return state

    def save(self) -> None:
        self._state["updated_at"] = int(time.time())
        self.store.set_kv(_kv_key(), self._state)

    def reload(self) -> None:
        self._state = self._load()

    # ------------------------------------------------------------ 读数
    @property
    def base(self) -> dict[str, float]:
        return {key: round(float(self._state["base"][key]), 2) for key in DIMS}

    @property
    def now(self) -> dict[str, float]:
        return {key: round(float(self._state["now"][key]), 2) for key in DIMS}

    def snapshot(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "now": self.now,
            "factory": dict(FACTORY),
            "dims": dict(DIMS),
            "narrative": self._state.get("narrative") or "",
            "narrative_at": self._state.get("narrative_at"),
            "marks": self.store.trait_marks(limit=10, persona=personas.active()),
            "stats": self.stats,
        }

    # ------------------------------------------------------------ 演化
    def nudge(
        self,
        deltas: dict[str, Any] | None,
        *,
        source: str = "chat",
        note: str = "",
        weight: float = 0.0,
    ) -> dict[str, Any]:
        """一次互动对性格的影响。

        - deltas: 维度名 -> -3~3（这轮对「当前状态」的推动）
        - weight: 0~1，这轮的分量。日常闲聊 0；被认真对待/被冷落/吵架和好 0.6~1
          只有 weight >= settings.traits_mark_min_weight 才会推动基线，并留一条年轮。
        """
        if not settings.personality_enabled or not deltas:
            return {"skipped": "disabled"}
        applied: dict[str, float] = {}
        for key, raw in (deltas or {}).items():
            if key not in DIMS:
                continue
            try:
                delta = float(raw)
            except (TypeError, ValueError):
                continue
            if not delta:
                continue
            delta = max(-3.0, min(3.0, delta))
            applied[key] = round(delta, 2)
            self._state["now"][key] = _clamp(
                float(self._state["now"][key]) + delta * settings.traits_now_gain
            )
        if not applied:
            return {"skipped": "no dims"}

        weight = max(0.0, min(1.0, float(weight or 0.0)))
        base_changed: dict[str, float] = {}
        if weight >= settings.traits_mark_min_weight:
            for key, delta in applied.items():
                base_delta = delta * weight * settings.traits_base_gain
                base_delta = max(-2.0, min(2.0, base_delta))
                if abs(base_delta) < 0.1:
                    continue
                new_value = _clamp(float(self._state["base"][key]) + base_delta)
                factory = FACTORY[key]
                new_value = max(factory - settings.traits_max_dev, min(factory + settings.traits_max_dev, new_value))
                base_changed[key] = round(new_value - float(self._state["base"][key]), 2)
                self._state["base"][key] = new_value
        if base_changed:
            before = {key: round(float(self._state["base"][key]) - base_changed[key], 2) for key in base_changed}
            after = {key: round(float(self._state["base"][key]), 2) for key in base_changed}
            self.store.add_trait_mark(
                persona=personas.active(),
                kind="event",
                source=source,
                note=note[:200],
                dims=base_changed,
                before=before,
                after=after,
            )
            self.stats["marks"] = int(self.stats.get("marks") or 0) + 1
            log.info("性格年轮 +%s 来源=%s 说明=%s", base_changed, source, note[:60])
        self.stats["nudges"] = int(self.stats.get("nudges") or 0) + 1
        self.save()
        return {"applied": applied, "base_changed": base_changed, "weight": weight}

    def settle(self, days: float = 1.0) -> dict[str, Any]:
        """每天一次：当前状态朝基线回；基线在长期无年轮后极慢回出厂。"""
        if not settings.personality_enabled:
            return {"skipped": "disabled"}
        days = max(0.0, float(days))
        rate = min(0.9, settings.traits_now_return * days)
        for key in DIMS:
            now = float(self._state["now"][key])
            base = float(self._state["base"][key])
            self._state["now"][key] = _clamp(now + (base - now) * rate)

        marks = self.store.trait_marks(limit=1, persona=personas.active())
        last_mark = float(marks[0]["at"]) if marks else 0.0
        quiet_days = (time.time() - last_mark) / 86400 if last_mark else 999
        if quiet_days >= settings.traits_base_quiet_days:
            for key in DIMS:
                base = float(self._state["base"][key])
                factory = FACTORY[key]
                if abs(base - factory) < 0.5:
                    continue
                step = settings.traits_base_return * days
                self._state["base"][key] = _clamp(base + (factory - base) * min(0.5, step / 10))
        self._state["last_settle"] = time.time()
        self.stats["settles"] = int(self.stats.get("settles") or 0) + 1
        self.save()
        return {"now": self.now, "base": self.base, "quiet_days": round(quiet_days, 1)}

    def reset(self) -> dict[str, Any]:
        self._state["base"] = dict(FACTORY)
        self._state["now"] = dict(FACTORY)
        self._state["narrative"] = ""
        self.save()
        return self.snapshot()

    def rollback(self, mark_id: int) -> dict[str, Any]:
        mark = self.store.get_trait_mark(mark_id)
        if not mark:
            return {"error": "mark not found"}
        dims = mark.get("dims") or {}
        for key, delta in dims.items():
            if key in DIMS:
                self._state["base"][key] = _clamp(float(self._state["base"][key]) - float(delta))
        self.store.delete_trait_mark(mark_id)
        self.save()
        return {"ok": True, "base": self.base}

    # ------------------------------------------------------------ 给大脑看
    def prompt_block(self) -> str:
        if not settings.personality_enabled:
            return ""
        b = self.base
        notes: list[str] = []
        if b["closeness"] >= 64:
            notes.append("你最近和他很近，愿意主动黏他、说想他")
        elif b["closeness"] <= 40:
            notes.append("你最近有点收着，不主动凑上去")
        if b["talkativeness"] <= 42:
            notes.append("你话不多，回得短，也少主动开话题")
        elif b["talkativeness"] >= 65:
            notes.append("你最近话挺多，愿意主动分享")
        if b["sensitivity"] >= 58:
            notes.append("你对语气很敏感，容易多想；但别把猜测说成事实")
        if b["tsundere"] >= 70:
            notes.append("你嘴硬心软，关心也常常反着说")
        if b["stability"] <= 45:
            notes.append("你最近情绪起伏偏大，缓过来也慢；低落时不要硬撑")
        lines = [
            "你最近的样子（长期倾向，不用每句都体现）："
            + "、".join(f"{DIMS[k]} {b[k]:.0f}" for k in DIMS)
        ]
        if notes:
            lines.append("；".join(notes) + "。")
        narrative = str(self._state.get("narrative") or "").strip()
        if narrative:
            lines.append(f"你最近对自己的感觉：{narrative}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 自我叙事
    async def write_narrative(self, *, force: bool = False) -> dict[str, Any]:
        """每 7 天写一段「我最近怎么了」，让长期变化被她自己记住。"""
        if not settings.personality_enabled or self.brain is None:
            return {"skipped": "disabled"}
        days = (time.time() - float(self._state.get("narrative_at") or 0)) / 86400
        if not force and days < settings.traits_narrative_days:
            return {"skipped": "too soon"}
        marks = self.store.trait_marks(limit=8, persona=personas.active())
        base = self.base
        dims_text = "、".join(f"{DIMS[k]}{base[k]:.0f}" for k in DIMS)
        marks_text = "\n".join(
            f"- {datetime.fromtimestamp(float(m['at'])).strftime('%m-%d')}："
            f"{(m.get('note') or m.get('source') or '')[:60]}（{json.dumps(m.get('dims') or {}, ensure_ascii=False)}）"
            for m in marks
        ) or "（最近没什么大事）"
        recent = self.store.memories(kind="identity", limit=2, persona=personas.active())
        prev = "\n".join(f"- {item.get('content')}" for item in recent) or "（无）"
        payload = (
            f"今天的日期：{datetime.now().strftime('%Y-%m-%d')}\n"
            f"你现在的性格：{dims_text}\n"
            f"最近的年轮：\n{marks_text}\n"
            f"以前写过的自我感受：\n{prev}"
        )
        from .brain import BrainError  # 避免循环导入

        try:
            out = await self.brain.complete(
                [
                    {
                        "role": "system",
                        "content": (
                            f"你是「{personas.name() or '她'}」的自我觉察。"
                            "只输出 JSON：{\"narrative\":\"60~120 字的第一人称感受\"}。"
                            "写法：像日记里的一段心里话，说这几周自己哪里变了（更黏/更收着/更容易多想/更稳），"
                            "可以提一两件具体的事，不要列表、不要总结腔、不要提数值。"
                        ),
                    },
                    {"role": "user", "content": payload},
                ],
                temperature=0.6,
                max_tokens=300,
            )
        except BrainError as exc:
            self.stats["last_error"] = str(exc)
            return {"error": str(exc)}
        from .memory import _parse_json  # 复用 JSON 提取

        data = _parse_json(out.get("text") or "")
        narrative = " ".join(str(data.get("narrative") or "").split()).strip()
        if not narrative:
            return {"error": "empty narrative"}
        self._state["narrative"] = narrative[:400]
        self._state["narrative_at"] = time.time()
        self.store.add_memory(
            narrative,
            kind="identity",
            source="narrative",
            salience=0.8,
            persona=personas.active(),
        )
        self.stats["narratives"] = int(self.stats.get("narratives") or 0) + 1
        self.save()
        return {"ok": True, "narrative": narrative}
