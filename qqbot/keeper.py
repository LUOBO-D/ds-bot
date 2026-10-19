"""心跳：让她在没有消息的时候也「活着」。

一拍（默认 15 分钟）做三件事：
1. 情绪向基线回落 + 昼夜精力；
2. 生活线推进一件小事（概率触发，深夜静默）；
3. 到点（默认 23:30）做日结：写 day_summary、沉淀事实。

**不主动发消息**——按当前设定，她只在你说话后回应，但她的状态在自己往前走。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from .config import settings
from .emotion import Mood
from .life import LifeService
from .memory import MemoryService
from .store import Store

log = logging.getLogger("qqbot.keeper")


class Keeper:
    def __init__(
        self,
        store: Store,
        mood: Mood,
        life: LifeService,
        memory: MemoryService,
        proactive: Any | None = None,
        social: Any | None = None,
    ) -> None:
        self.store = store
        self.mood = mood
        self.life = life
        self.memory = memory
        self.proactive = proactive
        self.social = social
        self._tasks: list[asyncio.Task[Any]] = []
        self.stats: dict[str, Any] = {
            "ticks": 0,
            "life_events": 0,
            "nightlies": 0,
            "proactive_actions": 0,
            "errors": 0,
            "started_at": None,
            "last_tick_at": None,
            "last_error": None,
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        self.stats["started_at"] = time.time()
        self._tasks = [asyncio.create_task(self._loop(), name="keeper-loop")]
        log.info(
            "心跳已启动 tick=%ss life=%s nightly=%s",
            settings.life_tick_seconds,
            settings.life_enabled,
            settings.nightly_time,
        )

    async def stop(self) -> None:
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _loop(self) -> None:
        # 启动后先补今天的日程，别等第一个 15 分钟
        try:
            await self.tick(bootstrap=True)
        except Exception:
            log.exception("心跳首拍失败")
        interval = max(30, int(settings.life_tick_seconds))
        while True:
            await asyncio.sleep(interval)
            try:
                await self.tick()
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.stats["last_error"] = repr(exc)
                log.exception("心跳异常")

    # ------------------------------------------------------------ 一拍
    async def tick(self, *, bootstrap: bool = False, force: bool = False) -> dict[str, Any]:
        self.stats["ticks"] += 1
        self.stats["last_tick_at"] = time.time()
        result: dict[str, Any] = {}
        if settings.mood_enabled:
            self.mood.decay()
        if settings.life_enabled or force:
            life_result = await self.life.tick(force=force)
            result["life"] = life_result
            if life_result.get("id"):
                self.stats["life_events"] += 1
        if not bootstrap:
            nightly = await self.maybe_nightly(force=force)
            if nightly:
                result["nightly"] = nightly
        if self.social is not None and settings.social_enabled:
            try:
                browsed = await self.social.tick(force=force)
                if browsed:
                    result["social"] = browsed
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.stats["last_error"] = repr(exc)
        if self.proactive is not None and settings.proactive_enabled:
            try:
                pro = await self.proactive.tick(force=force)
                if pro:
                    result["proactive"] = pro
                    self.stats["proactive_actions"] += sum(
                        1 for value in pro.values() if isinstance(value, dict) and value.get("ok")
                    )
            except Exception as exc:  # noqa: BLE001
                self.stats["errors"] += 1
                self.stats["last_error"] = repr(exc)
                log.exception("主动行为异常")
        return result

    # ------------------------------------------------------------ 日结
    async def maybe_nightly(self, *, force: bool = False) -> dict[str, Any] | None:
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        target = settings.nightly_time
        try:
            hour, minute = (int(part) for part in target.split(":", 1))
        except Exception:
            hour, minute = 23, 30
        if not force and (now.hour, now.minute) < (hour, minute):
            return None
        if not force and self.store.get_kv("nightly_done") == today:
            return None
        out = await self.memory.nightly(today)
        if out.get("summary"):
            self.store.set_kv("nightly_done", today)
            self.stats["nightlies"] += 1
        # 按人分开的日结：只给那个人看，互不串味
        per_user: dict[str, Any] = {}
        if out.get("summary") or force:
            for uid in self.store.active_user_ids(today)[:10]:
                # 先把当天还没压进备忘的消息补一次，让日结吃到完整的当天对话
                try:
                    await self.memory.update_memo(uid, name=str(uid), force=True)
                except Exception:  # noqa: BLE001
                    log.exception("日结前更新当天备忘失败 uid=%s", uid)
                result = await self.memory.nightly(today, uid=uid)
                per_user[str(uid)] = result.get("summary") or result.get("skipped") or result.get("error")
        if per_user:
            out = dict(out or {})
            out["per_user"] = per_user
            self.stats["nightlies"] += len(per_user)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self._tasks),
            "next_interval_seconds": settings.life_tick_seconds,
            "nightly_time": settings.nightly_time,
            "nightly_done": self.store.get_kv("nightly_done"),
            "mood": self.mood.snapshot(),
            "stats": self.stats,
        }
