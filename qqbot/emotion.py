"""情绪状态：愉悦度 valence、唤醒度 arousal、精力 energy。

三个量都在 0~1：

- valence：越高兴越高，0.5 左右是平常
- arousal：越激动/紧张越高，越低越慵懒
- energy：精力，受时间和作息影响，回一句消息会掉一点

设计要点：
1. **会漂**：聊天里的情绪起伏由反思环节给出 delta；
2. **会回落**：tick 时向基线缓慢回归，避免一次好心情永远挂着；
3. **有软边界**：越接近 1 越难再涨、越接近 0 越难再跌，防止长期顶格（以前会锁死在 1.0）；
4. **有情绪期**：跌到阈值以下会进入一段低落期（默认 6~48 小时的「这几天不太好」），
   带具体理由，到期自动回暖，最多 48 小时；
5. **有昼夜**：深夜精力自动掉，白天回升；
6. **影响说话方式**：不只是「心情描述」，还会给出语气指令（少撒娇、句子更短、不想多聊等）。

对外：snapshot()（给接口看）、prompt_block()（给大脑看）、style_tags()（结构化表达映射）。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any

from .config import settings
from . import personas
from .store import Store

log = logging.getLogger("qqbot.emotion")

DECAY_PER_TICK = 0.18


def _kv_key() -> str:
    """情绪按角色隔离：mood:<slug>。"""
    slug = personas.active() or "default"
    return f"mood:{slug}"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _soft_nudge(current: float, change: float) -> float:
    """靠近边界时让步幅变小：0.9 再想涨、0.1 再想跌，都会变得迟钝。"""
    if change >= 0:
        room = max(0.0, 1.0 - current) ** 0.6
    else:
        room = max(0.0, current) ** 0.6
    return _clamp(current + change * room)


class Mood:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._state: dict[str, Any] = self._load()

    def reload(self) -> None:
        """切换角色后重新载入该角色的情绪。"""
        self._state = self._load()

    # ------------------------------------------------------------ 存取
    def _defaults(self) -> dict[str, Any]:
        return {
            "valence": settings.mood_baseline_valence,
            "arousal": settings.mood_baseline_arousal,
            "energy": settings.mood_baseline_energy,
            "updated_at": int(time.time()),
            "note": "",
            "episode": None,
            "episode_history": [],
        }

    def _load(self) -> dict[str, Any]:
        raw = self.store.get_kv(_kv_key())
        state = self._defaults()
        if isinstance(raw, dict):
            for key in ("valence", "arousal", "energy"):
                try:
                    state[key] = _clamp(raw.get(key, state[key]))
                except (TypeError, ValueError):
                    pass
            state["updated_at"] = raw.get("updated_at") or state["updated_at"]
            state["note"] = str(raw.get("note") or "")
            episode = raw.get("episode")
            state["episode"] = episode if isinstance(episode, dict) else None
            history = raw.get("episode_history")
            state["episode_history"] = history if isinstance(history, list) else []
        return state

    def save(self) -> None:
        self._state["updated_at"] = int(time.time())
        self.store.set_kv(_kv_key(), self._state)

    # ------------------------------------------------------------ 情绪期
    @property
    def episode(self) -> dict[str, Any] | None:
        episode = self._state.get("episode")
        return episode if isinstance(episode, dict) else None

    def _start_episode(self, reason: str) -> None:
        if not settings.mood_episode_enabled:
            return
        if self.episode:
            return
        depth = max(0.0, float(settings.mood_episode_threshold) - self.valence)
        hours = settings.mood_episode_min_hours + depth * 60
        hours = min(float(settings.mood_episode_max_hours), max(float(settings.mood_episode_min_hours), hours))
        hours = min(hours, 48.0)
        now = time.time()
        self._state["episode"] = {
            "started_at": now,
            "until": now + hours * 3600,
            "hours": round(hours, 1),
            "reason": (reason or "说不上来，就是有点闷")[:120],
            "anchor": max(0.18, min(float(settings.mood_baseline_valence), 0.30)),
        }
        log.info("进入情绪期 %.1fh：%s", hours, reason)

    def _end_episode(self, *, recovered: bool = True) -> None:
        episode = self.episode
        if not episode:
            return
        history = list(self._state.get("episode_history") or [])[-4:]
        history.append(
            {
                "started_at": episode.get("started_at"),
                "ended_at": time.time(),
                "hours": episode.get("hours"),
                "reason": episode.get("reason"),
                "recovered": recovered,
            }
        )
        self._state["episode_history"] = history
        self._state["episode"] = None
        if recovered:
            self._state["note"] = "慢慢缓过来了"
            log.info("情绪期结束，回暖")

    # ------------------------------------------------------------ 演化
    def decay(self) -> None:
        """向基线回落 + 昼夜精力 + 情绪期到期回暖。心跳每拍调一次。"""
        now = time.time()
        episode = self.episode
        if episode and now >= float(episode.get("until") or 0):
            self._end_episode()

        base_v = settings.mood_baseline_valence
        base_a = settings.mood_baseline_arousal
        episode = self.episode
        for key, base in (("valence", base_v), ("arousal", base_a)):
            current = float(self._state.get(key, base))
            target = base
            if episode and key == "valence":
                # 情绪期里往低谷锚点漂，而不是回基线
                target = float(episode.get("anchor") or 0.28)
            rate = DECAY_PER_TICK * (0.55 if episode and key == "valence" else 1.0)
            self._state[key] = _clamp(current + (target - current) * rate)

        hour = datetime.now().hour
        if 0 <= hour < 7:
            energy_target = 0.30
        elif 7 <= hour < 9:
            energy_target = 0.55
        elif 9 <= hour < 20:
            energy_target = 0.78
        elif 20 <= hour < 23:
            energy_target = 0.62
        else:
            energy_target = 0.40
        energy = float(self._state.get("energy", energy_target))
        self._state["energy"] = _clamp(energy + (energy_target - energy) * 0.35)
        self.save()

    def apply_delta(self, delta: dict[str, Any] | None, *, note: str = "", scale: float = 1.0) -> None:
        """把 delta（-1~1）叠加到当前状态上（软边界，越接近极值越难动）。"""
        if not delta:
            return
        mapping = {
            "valence": "valence_delta",
            "arousal": "arousal_delta",
            "energy": "energy_delta",
        }
        episode = self.episode
        for key, src in mapping.items():
            try:
                change = float(delta.get(src) or delta.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if not change:
                continue
            current = float(self._state.get(key, 0.5))
            factor = scale * 0.5
            if episode and key == "valence" and change > 0:
                factor *= 0.6  # 低落期没那么容易被一句话哄好
            self._state[key] = _soft_nudge(current, change * factor)
        if note:
            self._state["note"] = str(note)[:120]
        if (
            settings.mood_episode_enabled
            and self.valence < float(settings.mood_episode_threshold)
            and not self.episode
        ):
            self._start_episode(note or "今天有点提不起劲")
        self.save()

    def spend(self, amount: float | None = None) -> None:
        """回一条消息消耗一点精力（会累，不是永动机）。"""
        cost = settings.mood_fatigue_per_reply if amount is None else float(amount)
        energy = float(self._state.get("energy", 0.6))
        self._state["energy"] = _clamp(energy - max(0.0, cost))
        self.save()

    def set(self, **values: Any) -> None:
        for key in ("valence", "arousal", "energy"):
            if values.get(key) is not None:
                self._state[key] = _clamp(values[key])
        if values.get("note") is not None:
            self._state["note"] = str(values["note"])[:120]
        if values.get("clear_episode"):
            self._end_episode(recovered=False)
        self.save()

    # ------------------------------------------------------------ 读数
    @property
    def valence(self) -> float:
        return float(self._state.get("valence", 0.5))

    @property
    def arousal(self) -> float:
        return float(self._state.get("arousal", 0.4))

    @property
    def energy(self) -> float:
        return float(self._state.get("energy", 0.7))

    def label(self) -> str:
        v, a, e = self.valence, self.arousal, self.energy
        if e < 0.35 and v < 0.45:
            return "困得不行"
        if v >= 0.62:
            return "挺开心的" if a < 0.62 else "有点兴奋"
        if v >= 0.45:
            return "心情平稳" if e >= 0.45 else "有点乏"
        if v >= 0.33:
            return "淡淡的" if e >= 0.45 else "有点累"
        return "不太开心" if a < 0.55 else "有点烦躁"

    def style_tags(self) -> dict[str, Any]:
        """把情绪翻译成可执行的说话方式（不是形容词）。"""
        v, e = self.valence, self.energy
        tags: dict[str, Any] = {
            "playful": v >= 0.62 and e >= 0.4,
            "selfie_sweet": v >= 0.68,
            "short": v < 0.42 or e < 0.38,
            "silent": v < 0.30,
            "flat": 0.30 <= v < 0.48,
            "tired": e < 0.40,
            "very_tired": e < 0.25,
            "episode": bool(self.episode),
            "pause_seconds": 0.0,
        }
        if tags["tired"]:
            tags["pause_seconds"] = 2.0
        if tags["very_tired"]:
            tags["pause_seconds"] = 3.5
        return tags

    def snapshot(self) -> dict[str, Any]:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "energy": round(self.energy, 3),
            "label": self.label(),
            "note": self._state.get("note") or "",
            "updated_at": self._state.get("updated_at"),
            "episode": self.episode,
            "episode_history": self._state.get("episode_history") or [],
            "style": self.style_tags(),
        }

    # ------------------------------------------------------------ 给大脑看
    def prompt_block(self) -> str:
        lines = [
            f"你现在的心情：{self.label()}"
            f"（愉悦 {self.valence:.2f}、唤醒 {self.arousal:.2f}、精力 {self.energy:.2f}）。"
        ]
        episode = self.episode
        if episode:
            lines.append(
                f"你这几天一直没太缓过来：{episode.get('reason') or '说不上来，就是有点闷'}。"
                "不是每轮都要强颜欢笑，也不用反复强调，让它自然渗在语气里。"
            )
        if settings.mood_style_map:
            lines.extend(self._style_lines())
        lines.append("情绪要真实地体现在语气和长短上，不用每次都强撑热情；但别用数值说话。")
        return "\n".join(lines)

    def _style_lines(self) -> list[str]:
        tags = self.style_tags()
        out: list[str] = []
        if tags["selfie_sweet"]:
            out.append("现在可以撒娇、叠字、偶尔一个颜文字，主动接话。")
        elif tags["playful"]:
            out.append("现在偏轻快，会接话、会逗他，但不腻。")
        if tags["flat"]:
            out.append("现在情绪是平的：少撒娇、少叠字，句子短一点，别硬撑出高兴的样子。")
        if tags["silent"]:
            out.append(
                "现在不太想多聊：可以回得很短、偶尔停一下，甚至说一句「今天不太想说话」。"
                "如果他问起，可以含糊但别骗他；不要因此怪他、冷暴力或阴阳怪气。"
            )
        if tags["tired"]:
            out.append("现在有点累：少分一条、回得慢一点，可以说「晚点再说」；别硬聊。")
        if tags["very_tired"]:
            out.append("现在很困，回复很短，允许没接完就犯困。")
        if not out:
            out.append("现在是很平常的状态，正常聊就好。")
        return out
