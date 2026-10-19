"""情绪状态：愉悦度 valence、唤醒度 arousal、精力 energy。

三个量都在 0~1：
- valence：越高兴越高，0.5 左右是平常
- arousal：越激动/紧张越高，越低越慵懒
- energy：精力，受时间和作息影响

设计要点：
1. **会漂**：聊天里的情绪起伏由反思环节给出 delta；
2. **会回落**：tick 时向基线缓慢回归，避免一次好心情永远挂着；
3. **有昼夜**：深夜精力自动掉，白天回升。

对外只有 snapshot()（给接口看）和 prompt_block()（给大脑看）。
"""

from __future__ import annotations

import logging
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


class Mood:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._state: dict[str, Any] = self._load()

    def reload(self) -> None:
        """切换角色后重新载入该角色的情绪。"""
        self._state = self._load()

    def _defaults(self) -> dict[str, Any]:
        return {
            "valence": settings.mood_baseline_valence,
            "arousal": settings.mood_baseline_arousal,
            "energy": settings.mood_baseline_energy,
            "updated_at": int(time.time()),
            "note": "",
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
        return state

    def save(self) -> None:
        self._state["updated_at"] = int(time.time())
        self.store.set_kv(_kv_key(), self._state)

    # ------------------------------------------------------------ 演化
    def decay(self) -> None:
        """向基线回落 + 昼夜精力。心跳每拍调一次。"""
        base_v = settings.mood_baseline_valence
        base_a = settings.mood_baseline_arousal
        for key, base in (("valence", base_v), ("arousal", base_a)):
            current = float(self._state.get(key, base))
            self._state[key] = _clamp(current + (base - current) * DECAY_PER_TICK)

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
        """把 delta（-1~1）叠加到当前状态上。"""
        if not delta:
            return
        mapping = {
            "valence": "valence_delta",
            "arousal": "arousal_delta",
            "energy": "energy_delta",
        }
        for key, src in mapping.items():
            try:
                change = float(delta.get(src) or delta.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if not change:
                continue
            current = float(self._state.get(key, 0.5))
            self._state[key] = _clamp(current + change * scale * 0.5)
        if note:
            self._state["note"] = str(note)[:120]
        self.save()

    def set(self, **values: Any) -> None:
        for key in ("valence", "arousal", "energy"):
            if values.get(key) is not None:
                self._state[key] = _clamp(values[key])
        if values.get("note") is not None:
            self._state["note"] = str(values["note"])[:120]
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "valence": round(self.valence, 3),
            "arousal": round(self.arousal, 3),
            "energy": round(self.energy, 3),
            "label": self.label(),
            "note": self._state.get("note") or "",
            "updated_at": self._state.get("updated_at"),
        }

    def prompt_block(self) -> str:
        return (
            f"你现在的心情：{self.label()}"
            f"（愉悦 {self.valence:.2f}、唤醒 {self.arousal:.2f}、精力 {self.energy:.2f}）。"
            "情绪要真实地体现在语气和长短上，不用每次都强撑热情；但别用数值说话。"
        )
