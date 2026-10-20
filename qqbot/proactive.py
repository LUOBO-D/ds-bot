"""主动行为：让她自己找存在感。

两类动作，都受开关与频率限制：
1. 戳一戳（poke）：他很久没说话、她又想他的时候，戳他一下，多数时候再补一句很短的话。
2. 空间说说：每天最多一条，把当天生活线写成一条 QQ 空间动态发出去。

设计原则：宁可不发，也不刷屏；每个动作都写审计、写生活日志、可随时关掉。
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Any

from .config import settings

log = logging.getLogger("qqbot.proactive")


def _p() -> str:
    from . import personas

    return personas.active()

POKE_LINE_PROMPT = (
    "（系统提示）你现在有点想他，主动戳了他一下。"
    "写一句很短的话发给他：不超过 15 个字，口语，像随手打的，不要解释，不要加引号。"
)

FEED_PROMPT = (
    "（系统提示）刚发生的事：{reason}。\n"
    "你现在可能想发一条 QQ 空间说说，也可能不想——自己判断，不要为了发而发。\n"
    "如果想发：只输出说说正文，第一人称，30~80 字，像随手记的，可以带一点情绪；"
    "不要话题标签，不要艾特人，不要提 AI，别用「今天」开头的模板句。\n"
    "如果不想发：只输出 NO。\n"
    "只输出正文或 NO，不要解释。"
)

FEED_NO_TOKENS = ("NO", "N", "不", "不想", "算了", "没什么想发")

MESSAGE_PROMPT = (
    "（系统提示）你现在想主动找他说话——不是回复，是你自己忽然想起他。\n"
    "结合你今天在过的日子和此刻的心情，写一条很短的消息：第一人称，不超过 30 字，"
    "要具体（说出你正在做什么、看到什么、想到什么），不要「在吗」「在干嘛」这种问候模板，"
    "不要解释你为什么发。\n"
    "再配一个画面描述，用来生成一张「随手拍」的照片：只拍场景或物件，不出现人、不出现文字，"
    "例如窗边的杯子、灯下的书、楼下的树、饭桌一角。\n"
    "只输出 JSON：{\"text\":\"消息正文\",\"scene\":\"画面描述\"}，不要输出别的。"
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class Proactive:
    def __init__(self, store: Any, actions: Any, *, brain: Any = None, mood: Any = None,
                 life: Any = None, qzone: Any = None, imagegen: Any = None) -> None:
        self.store = store
        self.actions = actions
        self.brain = brain
        self.mood = mood
        self.life = life
        self.qzone = qzone
        self.imagegen = imagegen
        self.rng = random.Random()
        self.stats: dict[str, Any] = {
            "pokes": 0,
            "poke_skipped": 0,
            "feeds": 0,
            "feed_skipped": 0,
            "errors": 0,
            "last_poke_at": None,
            "last_poke_text": None,
            "last_feed_at": None,
            "last_feed_text": None,
            "messages": 0,
            "message_skipped": 0,
                "images": 0,
            "image_failed": 0,
            "last_message_at": None,
            "last_message_text": None,
            "last_error": None,
        }

    # ------------------------------------------------------------ 小工具

    def _roll_day(self) -> None:
        today = _today()
        if self.store.get_kv("proactive.day") != today:
            self.store.set_kv("proactive.day", today)
            self.store.set_kv("proactive.pokes", 0)
            self.store.set_kv("proactive.feeds", 0)

    def _int_kv(self, key: str) -> int:
        try:
            return int(self.store.get_kv(key) or 0)
        except (TypeError, ValueError):
            return 0

    def _float_kv(self, key: str) -> float | None:
        value = self.store.get_kv(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _in_window(start: int, end: int) -> bool:
        hour = datetime.now().hour
        if start <= end:
            return start <= hour < end
        return hour >= start or hour < end

    # ------------------------------------------------------------ 戳一戳

    def poke_status(self) -> dict[str, Any]:
        last = self._float_kv("proactive.last_poke_at")
        return {
            "enabled": settings.poke_enabled and settings.proactive_enabled,
            "target": settings.poke_target,
            "pokes_today": self._int_kv("proactive.pokes"),
            "daily_max": settings.poke_daily_max,
            "cooldown_seconds": settings.poke_cooldown_seconds,
            "idle_seconds": settings.poke_idle_seconds,
            "window": [settings.poke_window_start, settings.poke_window_end],
            "last_poke_at": last,
            "seconds_since_poke": round(time.time() - last) if last else None,
        }

    def _poke_block_reason(self, *, force: bool) -> str | None:
        if not settings.proactive_enabled:
            return "主动行为总开关关闭"
        if not settings.poke_enabled and not force:
            return "戳一戳开关关闭"
        if self._int_kv("proactive.pokes") >= settings.poke_daily_max and not force:
            return "今日戳一戳次数已达上限"
        last = self._float_kv("proactive.last_poke_at")
        if last and time.time() - last < settings.poke_cooldown_seconds and not force:
            return "冷却中"
        if not force and not self._in_window(settings.poke_window_start, settings.poke_window_end):
            return "不在允许时段"
        return None

    async def poke(self, *, reason: str = "manual", force: bool = False,
                   with_message: bool | None = None) -> dict[str, Any]:
        block = self._poke_block_reason(force=force)
        if block:
            self.stats["poke_skipped"] += 1
            return {"ok": False, "skipped": block}
        self._roll_day()
        target = settings.poke_target
        try:
            result = await self.actions.poke("private", target)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("戳一戳失败")
            return {"ok": False, "error": repr(exc)}

        text = ""
        want_message = settings.poke_message_probability >= self.rng.random()
        if with_message is not None:
            want_message = with_message
        if want_message and self.brain is not None:
            text = await self._compose(POKE_LINE_PROMPT, max_tokens=60, temperature=1.0)
            if text:
                try:
                    await self.actions.send_text("private", target, text)
                except Exception:  # noqa: BLE001
                    log.exception("戳一戳后补话失败")

        self.store.set_kv("proactive.last_poke_at", time.time())
        self.store.set_kv("proactive.pokes", self._int_kv("proactive.pokes") + 1)
        self.stats["pokes"] += 1
        self.stats["last_poke_at"] = time.time()
        self.stats["last_poke_text"] = text
        self.store.add_life(
            f"我戳了他一下（{reason}）" + (f"，跟他说：{text}" if text else ""),
            day=_today(),
            kind="action",
            mood=self.mood.label() if self.mood else None,
        persona=_p(),
    )
        self.store.audit("proactive", "poke", {"target": target, "reason": reason}, result, True)
        log.info("主动戳一戳 target=%s reason=%s text=%r", target, reason, text)
        return {"ok": True, "result": result, "text": text}

    # ------------------------------------------------------------ 空间说说

    def feed_status(self) -> dict[str, Any]:
        last = self._float_kv("proactive.last_feed_at")
        return {
            "enabled": settings.qzone_auto_feed_enabled and settings.proactive_enabled,
            "qzone_ready": self.qzone is not None,
            "feeds_today": self._int_kv("proactive.feeds"),
            "daily_max": settings.qzone_feed_daily_max,
            "min_hours": settings.qzone_feed_min_hours,
            "window": [settings.qzone_feed_window_start, settings.qzone_feed_window_end],
            "last_feed_at": last,
            "seconds_since_feed": round(time.time() - last) if last else None,
        }

    def _feed_block_reason(self, *, force: bool) -> str | None:
        if not settings.proactive_enabled:
            return "主动行为总开关关闭"
        if not settings.qzone_auto_feed_enabled and not force:
            return "空间说说开关关闭"
        if self.qzone is None:
            return "空间 sidecar 未接入"
        if self._int_kv("proactive.feeds") >= settings.qzone_feed_daily_max and not force:
            return "今天已经发过了"
        last = self._float_kv("proactive.last_feed_at")
        if last and time.time() - last < settings.qzone_feed_min_hours * 3600 and not force:
            return "距上一条不足最小间隔"
        if not force and not self._in_window(settings.qzone_feed_window_start, settings.qzone_feed_window_end):
            return "不在允许时段"
        return None

    async def _decide_feed(self, reason: str) -> str:
        """一次调用同时完成「想不想发」和「写什么」：不想发回空串。"""
        prompt = FEED_PROMPT.format(reason=reason)
        text = await self._compose(prompt, max_tokens=220, temperature=1.0)
        if not text or text.upper().startswith("NO") or text in FEED_NO_TOKENS:
            return ""
        if len(text) < 6:
            return ""
        return text

    async def maybe_feed(self, reason: str, *, force: bool = False) -> dict[str, Any]:
        """按她自己的判断决定要不要发一条说说（不是定时任务）。"""
        block = self._feed_block_reason(force=force)
        if block:
            self.stats["feed_skipped"] += 1
            return {"ok": False, "skipped": block}
        self._roll_day()
        text = await self._decide_feed(reason)
        if not text:
            self.stats["feed_skipped"] += 1
            self.stats["last_feed_decision"] = "skip"
            return {"ok": False, "skipped": "她觉得这会儿没什么想发的"}
        return await self._publish_feed(text, reason=reason)

    async def preview_feed(self, reason: str = "manual") -> dict[str, Any]:
        """只做「想不想发 / 写什么」的判断，不真的发布（未登录时也能测）。"""
        text = await self._decide_feed(reason)
        return {"ok": bool(text), "would_post": bool(text), "text": text, "reason": reason}

    async def _publish_feed(self, text: str, *, reason: str) -> dict[str, Any]:
        if self.qzone is None:
            return {"ok": False, "skipped": "空间 sidecar 未接入"}
        try:
            login = await self.qzone.login_info()
        except Exception as exc:  # noqa: BLE001
            self.stats["feed_skipped"] += 1
            return {"ok": False, "skipped": f"空间桥接不可用：{exc!r}"}
        user_id = None
        if isinstance(login, dict):
            user_id = login.get("user_id") or login.get("uin")
        if not user_id:
            self.stats["feed_skipped"] += 1
            return {"ok": False, "skipped": "空间还没登录，先不发了"}
        try:
            result = await self.qzone.publish(text)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("发布空间说说失败")
            return {"ok": False, "error": repr(exc)}
        self.store.set_kv("proactive.last_feed_at", time.time())
        self.store.set_kv("proactive.feeds", self._int_kv("proactive.feeds") + 1)
        self.stats["feeds"] += 1
        self.stats["last_feed_at"] = time.time()
        self.stats["last_feed_text"] = text
        self.stats["last_feed_decision"] = "post"
        self.store.add_life(
            f"我在空间发了一条说说：{text}",
            day=_today(),
            kind="feed",
            mood=self.mood.label() if self.mood else None,
        persona=_p(),
    )
        self.store.audit("proactive", "qzone_feed", {"reason": reason, "text": text}, result, True)
        log.info("发布空间说说 reason=%s text=%r", reason, text)
        return {"ok": True, "text": text, "result": result}

    async def feed(self, *, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """兼容旧接口：force=True 时仍然先让她判断要不要发。"""
        return await self.maybe_feed(reason, force=force)

    # ------------------------------------------------------------ 组合

    def _context_text(self) -> str:
        blocks: list[str] = []
        if self.mood is not None:
            blocks.append(self.mood.prompt_block())
        if self.life is not None:
            try:
                blocks.append(self.life.context_block(_today(), limit=3))
            except Exception:  # noqa: BLE001
                log.exception("拼生活线失败")
        blocks.append(f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        return "\n\n".join(block for block in blocks if block)

    async def _compose(self, instruction: str, *, max_tokens: int, temperature: float) -> str:
        if self.brain is None:
            return ""
        messages = [
            {"role": "system", "content": self.brain.persona()},
            {"role": "system", "content": self._context_text()},
            {"role": "user", "content": instruction},
        ]
        try:
            out = await self.brain.complete(messages, temperature=temperature, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.warning("主动文案生成失败: %r", exc)
            return ""
        text = (out.get("text") or "").strip().strip("「」\"'")
        return text.split("\n")[0][:120] if text else ""

    # ------------------------------------------------------------ 自发私聊
    def _image_quota_left(self) -> bool:
        if self.imagegen is None or not getattr(settings, "imagegen_enabled", False):
            return False
        today = _today()
        if self.store.get_kv("imagegen.day") != today:
            self.store.set_kv("imagegen.day", today)
            self.store.set_kv("imagegen.count", 0)
        return self._int_kv("imagegen.count") < int(settings.imagegen_max_per_day)

    def _target_uid(self) -> int:
        return int(settings.proactive_message_target or settings.owner_uid or 0)

    def _message_block_reason(self, *, force: bool) -> str | None:
        if not (settings.proactive_enabled and settings.proactive_message_enabled):
            return "自发私聊没开"
        if not force and not self._in_window(
            settings.proactive_message_window_start, settings.proactive_message_window_end
        ):
            return "不在允许主动说话的时间段"
        target = self._target_uid()
        if not target:
            return "没有配置收件人（PROACTIVE_MESSAGE_TARGET 或 OWNER_UID）"
        if not force and self._int_kv("proactive.messages") >= settings.proactive_message_daily_max:
            return "今天的主动额度用完了"
        last_sent = self._float_kv("proactive.last_message_at")
        if last_sent and time.time() - last_sent < settings.proactive_message_cooldown_seconds and not force:
            return "离上次主动太近"
        last_in = self.store.last_message_at(user_id=target, direction="in")
        if not force and (last_in is None or time.time() - last_in < settings.proactive_message_idle_seconds):
            return "他最近还在说话"
        if last_sent and (last_in is None or last_in < last_sent) and not force:
            return "上一条主动还没被回，先不追"
        return None

    def _motivation(self) -> float:
        score = 0.35
        target = self._target_uid()
        last_in = self.store.last_message_at(user_id=target, direction="in") if target else None
        if last_in:
            idle_hours = (time.time() - last_in) / 3600
            score += min(0.3, idle_hours / 10.0 * 0.3)
        if self.mood is not None:
            mood = self.mood.snapshot()
            score += 0.2 * (1.0 - float(mood.get("valence") or 0.5))
            score += 0.05 * (1.0 - float(mood.get("energy") or 0.5))
        if self.life is not None:
            try:
                if self.store.life_log(day=_today(), limit=1, persona=_p()):
                    score += 0.1
            except Exception:  # noqa: BLE001
                pass
        return max(0.0, min(1.0, score))

    @staticmethod
    def _json_block(text: str) -> dict[str, Any]:
        import json as _json
        import re as _re

        match = _re.search(r"\{.*\}", text or "", _re.S)
        if not match:
            return {}
        try:
            data = _json.loads(match.group(0))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    async def _compose_message(self) -> dict[str, Any]:
        if self.brain is None:
            return {}
        messages = [
            {"role": "system", "content": self.brain.persona()},
            {"role": "system", "content": self._context_text()},
            {"role": "user", "content": MESSAGE_PROMPT},
        ]
        try:
            out = await self.brain.complete(messages, temperature=0.9, max_tokens=220)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.warning("主动私聊文案生成失败: %r", exc)
            return {}
        data = self._json_block(out.get("text") or "")
        text = str(data.get("text") or "").strip()
        scene = str(data.get("scene") or "").strip()
        if not text:
            lines = (out.get("text") or "").strip().splitlines()
            text = lines[0][:80] if lines else ""
        return {"text": text, "scene": scene}

    async def send_message(self, *, reason: str = "tick", force: bool = False,
                           dry_run: bool = False) -> dict[str, Any]:
        block = self._message_block_reason(force=force)
        if block:
            self.stats["message_skipped"] += 1
            return {"ok": False, "skipped": block}
        if not force and self.rng.random() > self._motivation():
            self.stats["message_skipped"] += 1
            return {"ok": False, "skipped": "这会儿没那么想问"}
        composed = await self._compose_message()
        text = (composed.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "文案生成失败"}
        image: dict[str, Any] | None = None
        if self._image_quota_left():
            try:
                image = await self.imagegen.generate(
                    composed.get("scene") or text,
                    mood=self.mood.label() if self.mood else "",
                )
            except Exception:  # noqa: BLE001
                log.exception("主动消息配图失败")
                image = None
            if image is None:
                self.stats["image_failed"] += 1
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "reason": reason,
                "text": text,
                "scene": composed.get("scene"),
                "image": image,
            }
        target = self._target_uid()
        sent: list[Any] = []
        if image:
            sent.append(await self.actions.send_image("private", target, image["path"]))
            self.stats["images"] += 1
            self.store.set_kv("imagegen.count", self._int_kv("imagegen.count") + 1)
        sent.append(await self.actions.send_text("private", target, text))
        self.store.set_kv("proactive.last_message_at", time.time())
        self.store.set_kv("proactive.messages", self._int_kv("proactive.messages") + 1)
        self.stats["messages"] += 1
        self.stats["last_message_at"] = time.time()
        self.stats["last_message_text"] = text
        self.store.add_life(
            ("我给他发了张照片，说：" if image else "我主动给他发消息：") + text,
            day=_today(),
            kind="message",
            mood=self.mood.label() if self.mood else None,
            persona=_p(),
        )
        self.store.audit(
            "proactive",
            "private_message",
            {"reason": reason, "text": text, "image": bool(image)},
            {"sent": [item.get("message_id") for item in sent]},
            True,
        )
        log.info("主动私聊 reason=%s image=%s text=%r", reason, bool(image), text)
        return {"ok": True, "text": text, "image": image, "sent": sent}

    async def tick(self, *, force: bool = False) -> dict[str, Any]:
        self._roll_day()
        out: dict[str, Any] = {}
        if self._poke_due():
            out["poke"] = await self.poke(reason="idle", force=force)
        if self._feed_due():
            out["feed"] = await self.maybe_feed("心跳里想起点什么", force=force)
        try:
            if not self._message_block_reason(force=force):
                result = await self.send_message(reason="tick", force=force)
                if result.get("ok"):
                    out["message"] = result
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("主动私聊失败")
        return out

    async def on_chat_finished(self, *, summary: str = "") -> dict[str, Any] | None:
        """一次对话结束后：她自己判断要不要把这点心情发到空间。"""
        if not (settings.proactive_enabled and settings.qzone_auto_feed_enabled and self.qzone is not None):
            return None
        self._roll_day()
        if self.rng.random() >= settings.qzone_feed_chat_probability:
            return None
        reason = "刚和他聊完天" + (f"（{summary}）" if summary else "")
        return await self.maybe_feed(reason)

    def _poke_due(self) -> bool:
        if not (settings.proactive_enabled and settings.poke_enabled):
            return False
        if self._int_kv("proactive.pokes") >= settings.poke_daily_max:
            return False
        last = self._float_kv("proactive.last_poke_at")
        if last and time.time() - last < settings.poke_cooldown_seconds:
            return False
        if not self._in_window(settings.poke_window_start, settings.poke_window_end):
            return False
        last_in = self.store.last_message_at(user_id=settings.poke_target, direction="in")
        idle = time.time() - (last_in or 0)
        if idle < settings.poke_idle_seconds:
            return False
        # 到点了也只是一半概率：宁可少打扰
        return self.rng.random() < 0.5

    def _feed_due(self) -> bool:
        if not (settings.proactive_enabled and settings.qzone_auto_feed_enabled and self.qzone is not None):
            return False
        if self._int_kv("proactive.feeds") >= settings.qzone_feed_daily_max:
            return False
        last = self._float_kv("proactive.last_feed_at")
        if last and time.time() - last < settings.qzone_feed_min_hours * 3600:
            return False
        if not self._in_window(settings.qzone_feed_window_start, settings.qzone_feed_window_end):
            return False
        return self.rng.random() < settings.qzone_feed_impulse_probability

    def message_status(self) -> dict[str, Any]:
        return {
            "enabled": bool(settings.proactive_enabled and settings.proactive_message_enabled),
            "target": self._target_uid(),
            "daily_max": settings.proactive_message_daily_max,
            "today": self._int_kv("proactive.messages"),
            "unanswered": bool(
                (self._float_kv("proactive.last_message_at") or 0)
                > (self.store.last_message_at(user_id=self._target_uid(), direction="in") or 0)
            ),
            "window": [settings.proactive_message_window_start, settings.proactive_message_window_end],
            "cooldown_seconds": settings.proactive_message_cooldown_seconds,
            "image_quota_left": self._image_quota_left(),
            "image_enabled": bool(self.imagegen is not None and settings.imagegen_enabled),
        }

    def status(self) -> dict[str, Any]:
        return {
            "enabled": settings.proactive_enabled,
            "poke": self.poke_status(),
            "feed": self.feed_status(),
            "message": self.message_status(),
            "imagegen": self.imagegen.status() if self.imagegen is not None else None,
            "stats": self.stats,
        }
