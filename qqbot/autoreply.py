"""实时自动回复。

流程：NapCat 反向 WS 事件 → `_consume_events` 落库并广播 → 本模块收到的私聊消息
→ 合并 1.2s 内连发的几句 → 带历史上下文调大脑 → 直接回复。

只在 `AUTO_REPLY_ALLOWLIST` 里的 QQ 号会触发（默认空 = 不自动回，需在 .env 配置）。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import personas
from .brain import Brain, BrainError
from .config import settings
from .onebot import OneBotClient
from .store import Store
from .tools import Actions

log = logging.getLogger("qqbot.autoreply")

MEMORY_RE = re.compile(r"(?:记住|记一下|记着|别忘)[了：:，,\s]*(.{2,200})")


EMOTION_HINTS = {
    "<|HAPPY|>": "他这条语音的语气听着挺开心",
    "<|SAD|>": "他这条语音的语气有点低落",
    "<|ANGRY|>": "他这条语音的语气有点冲/急",
    "<|NEUTRAL|>": "",
    "<|FEARFUL|>": "他这条语音的语气有点慌",
    "<|DISGUSTED|>": "他这条语音的语气有点不耐烦",
    "<|SURPRISED|>": "他这条语音的语气有点惊讶",
}


def _emotion_hint(meta: dict[str, Any]) -> str:
    """把 SenseVoice 的情绪 token 变成一句给大脑的旁白（没有就不加）。"""
    token = str(meta.get("emotion") or "")
    hint = EMOTION_HINTS.get(token, "")
    return f"（{hint}）" if hint else ""


def _too_similar(bubbles: list[str], avoid: list[str], *, threshold: float = 0.8) -> bool:
    """这一版回复是不是和她说过的某句几乎一样（复读检测）。"""
    if not bubbles or not avoid:
        return False
    newest = "".join(bubbles)
    if len(newest) < 4:
        return False
    for old in avoid:
        old = (old or "").strip()
        if len(old) < 4:
            continue
        if difflib.SequenceMatcher(None, newest, old).ratio() >= threshold:
            return True
        for bubble in bubbles:
            if len(bubble) >= 4 and difflib.SequenceMatcher(None, bubble, old).ratio() >= threshold:
                return True
    return False


def segment_text(seg: dict[str, Any]) -> str:
    """把一个 OneBot 消息段转成给大脑看的占位文本。"""
    if not isinstance(seg, dict):
        return str(seg)
    kind = seg.get("type") or ""
    data = seg.get("data") or {}
    if kind == "text":
        return str(data.get("text") or "")
    if kind == "at":
        qq = data.get("qq")
        return " @全体成员 " if qq == "all" else f" @{qq} "
    if kind == "image":
        return "[表情包]" if str(data.get("sub_type")) == "1" else "[图片]"
    if kind in ("mface", "face"):
        return "[表情包]" if kind == "mface" else "[表情]"
    if kind == "record":
        return "[语音]"
    if kind == "video":
        return "[视频]"
    if kind == "file":
        return "[文件]"
    if kind == "json":
        return "[卡片]"
    if kind == "forward":
        return "[聊天记录]"
    if kind == "poke":
        return "[戳一戳]"
    if kind == "dice":
        return "[骰子]"
    if kind == "rps":
        return "[猜拳]"
    if kind == "reply":
        return ""
    return f"[{kind}]"


def _looks_like_command(text: str) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 40:
        return False
    return any(t.startswith(key) for key in ("切换角色", "角色列表", "当前角色", "切回", "换回"))


def message_text(event: dict[str, Any]) -> str:
    segments = event.get("message")
    if isinstance(segments, list):
        text = "".join(segment_text(seg) for seg in segments)
    else:
        text = str(event.get("raw_message") or "")
    return " ".join(text.split()).strip()


class AutoReply:
    def __init__(
        self,
        ob: OneBotClient,
        store: Store,
        actions: Actions,
        brain: Brain,
        memory: Any | None = None,
        mood: Any | None = None,
        life: Any | None = None,
        proactive: Any | None = None,
        vision: Any | None = None,
        social: Any | None = None,
        asr: Any | None = None,
        stickers: Any | None = None,
    ) -> None:
        self.ob = ob
        self.store = store
        self.actions = actions
        self.brain = brain
        self.memory = memory
        self.mood = mood
        self.life = life
        self.proactive = proactive
        self.vision = vision
        self.social = social
        self.asr = asr
        self.stickers = stickers
        self.rng = random.Random()
        self.enabled = settings.autoreply_enabled
        raw = [str(item).strip() for item in settings.autoreply_allowlist]
        self.allowlist = {int(uid) for uid in raw if uid.isdigit()}
        self.allowlist_all_friends = any(item.lower() in ("*", "all", "friends") for item in raw)
        self._friends: set[int] = set()
        self._friends_at = 0.0
        self._pending: dict[int, dict[str, Any]] = {}
        self._seen: list[int] = []
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._tasks: list[asyncio.Task[Any]] = []
        self._locks: dict[int, asyncio.Lock] = {}
        self._reflect_tasks: set[asyncio.Task[Any]] = set()
        self.stats: dict[str, Any] = {
            "received": 0,
            "replied": 0,
            "skipped": 0,
            "errors": 0,
            "last_reply_at": None,
            "last_error": None,
            "last_latency_ms": None,
        }

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        if not self.brain.ready:
            log.warning("未配置 DEEPSEEK_API_KEY，自动回复不可用")
        self._queue = asyncio.Queue(maxsize=500)
        from . import main  # 延迟导入避免循环

        main.subscribers.add(self._queue)
        self._tasks = [
            asyncio.create_task(self._listen(), name="autoreply-listen"),
            asyncio.create_task(self._debounce_loop(), name="autoreply-debounce"),
        ]
        scope = "所有好友" if self.allowlist_all_friends else sorted(self.allowlist)
        log.info("自动回复已启用 allowlist=%s gap=%.1fs model=%s", scope, settings.autoreply_gap, self.brain.model)

    async def stop(self) -> None:
        if self._queue is not None:
            from . import main

            main.subscribers.discard(self._queue)
        pending, self._reflect_tasks = list(self._reflect_tasks), set()
        for task in pending:
            task.cancel()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks + pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # ------------------------------------------------------------ 事件入口
    async def _listen(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            try:
                await self._on_event(event)
            except Exception:
                log.exception("autoreply 处理事件失败")

    async def _allowed(self, uid: int) -> bool:
        if uid in self.allowlist:
            return True
        if not self.allowlist_all_friends:
            return False
        return uid in await self._friend_ids()

    async def _friend_ids(self) -> set[int]:
        now = time.time()
        if self._friends and now - self._friends_at < 600:
            return self._friends
        try:
            data = await self.ob.get_friend_list()
        except Exception:
            log.exception("拉好友列表失败，沿用上次缓存")
            return self._friends
        ids: set[int] = set()
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                value = int(row.get("user_id") or row.get("uin") or 0)
            except (TypeError, ValueError):
                continue
            if value:
                ids.add(value)
        self._friends = ids
        self._friends_at = now
        log.info("好友列表已刷新：%s 人", len(ids))
        return ids

    async def _on_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if event.get("post_type") != "message":
            return
        if event.get("message_type") != "private":
            self.stats["skipped"] += 1
            return
        uid = int(event.get("user_id") or 0)
        if not uid or uid == self.ob.self_id:
            return
        if not await self._allowed(uid):
            self.stats["skipped"] += 1
            log.info("跳过自动回复：%s 不在允许范围", uid)
            return
        message_id = event.get("message_id")
        if message_id in self._seen:
            return
        self._seen.append(message_id)
        if len(self._seen) > 500:
            del self._seen[:250]

        segments = event.get("message") if isinstance(event.get("message"), list) else []
        has_media = any(
            isinstance(seg, dict)
            and seg.get("type") in ("image", "mface")
            and bool((seg.get("data") or {}).get("url"))
            for seg in segments
        )
        text = message_text(event)
        if not text and not has_media:
            return
        if text and _looks_like_command(text):
            self.stats["received"] += 1
            log.info("收到角色指令 user=%s text=%r", uid, text[:40])
            asyncio.create_task(self._handle_command(uid, text.strip()))
            return
        self.stats["received"] += 1
        nick = ((event.get("sender") or {}).get("nickname") or str(uid)).strip()
        slot = self._pending.setdefault(uid, {"parts": [], "last": 0.0, "name": nick, "busy": False})
        slot.setdefault("events", []).append(event)
        if text:
            slot["parts"].append(text)
        slot["name"] = nick
        slot["last"] = time.time()
        log.info("收到待回复消息 user=%s mid=%s text=%r", uid, message_id, text[:80])

    async def _handle_command(self, uid: int, text: str) -> None:
        """角色切换类指令：不经过大脑，直接回执。"""
        try:
            t = text.strip()
            if t.startswith("角色列表"):
                items = personas.listing()
                lines = [
                    f"{index + 1}. {item['name']}" + ("（当前）" if item["active"] else "")
                    for index, item in enumerate(items)
                ]
                await self.actions.send_text(
                    "private", uid, "角色列表：\n" + "\n".join(lines) + "\n\n说「切换角色 名字」就能换。"
                )
                return
            if t.startswith("当前角色"):
                await self.actions.send_text("private", uid, f"现在是 {personas.name()}。")
                return
            if t.startswith("切回") or t.startswith("换回"):
                slug = personas.FALLBACK_PERSONA if personas.exists(personas.FALLBACK_PERSONA) else None
            else:
                for prefix in ("切换角色", "切到", "换成", "扮演", "变身"):
                    if t.startswith(prefix):
                        raw = t[len(prefix):].strip()
                        break
                else:
                    raw = t
                slug = personas.resolve(raw)
            if not slug:
                await self.actions.send_text("private", uid, "没找到这张角色卡，说「角色列表」看看有哪些。")
                return
            personas.set_active(slug)
            self.mood.reload()
            self.stats["switches"] = int(self.stats.get("switches") or 0) + 1
            self.stats["persona"] = slug
            log.info("切换角色 -> %s (user=%s)", slug, uid)
            await self.actions.send_text("private", uid, f"换好了，现在是 {personas.name(slug)}。")
        except Exception:
            log.exception("角色指令执行失败")

    async def _debounce_loop(self) -> None:
        while True:
            await asyncio.sleep(0.15)
            if not self._pending:
                continue
            now = time.time()
            for uid, slot in list(self._pending.items()):
                if slot["busy"]:
                    continue
                lock = self._locks.setdefault(uid, asyncio.Lock())
                if lock.locked():
                    continue
                if now - slot["last"] < settings.autoreply_gap:
                    continue
                slot["busy"] = True
                asyncio.create_task(self._respond(uid, slot, lock), name=f"autoreply-{uid}")

    # ------------------------------------------------------------ 回复
    async def _respond(self, uid: int, slot: dict[str, Any], lock: asyncio.Lock) -> None:
        async with lock:
            await self._respond_locked(uid, slot)

    async def _event_text(self, event: dict[str, Any]) -> str:
        """把一条消息（含图片 / 表情包）转成给大脑看的一行文本；图片先识别再写进去。"""
        segments = event.get("message")
        if not isinstance(segments, list):
            return " ".join(str(event.get("raw_message") or "").split())
        uid = int(event.get("user_id") or 0)
        message_id = event.get("message_id")
        out: list[str] = []
        enriched = False
        for seg in segments:
            if not isinstance(seg, dict):
                out.append(str(seg))
                continue
            kind = seg.get("type") or ""
            data = seg.get("data") or {}
            if kind in ("image", "mface"):
                sticker = kind == "mface" or str(data.get("sub_type")) == "1"
                label = "表情包" if sticker else "图片"
                url = data.get("url") or ""
                desc = None
                if url and self.vision is not None:
                    try:
                        desc = await self.vision.describe_url(
                            url,
                            kind="sticker" if sticker else "image",
                            sender_id=uid,
                            message_id=message_id,
                        )
                    except Exception:
                        log.exception("识图失败 mid=%s", message_id)
                out.append(f"[{label}：{desc}]" if desc else f"[{label}]")
                if sticker and desc and self.stickers is not None:
                    try:
                        await self.stickers.consider(
                            url=url,
                            desc=desc,
                            sender_id=uid,
                            message_id=message_id,
                        )
                    except Exception:  # noqa: BLE001
                        log.exception("表情包入库失败 mid=%s", message_id)
                enriched = enriched or bool(desc)
                continue
            if kind == "record":
                text = ""
                meta: dict[str, Any] = {}
                if self.asr is not None:
                    local = data.get("path") or ""
                    try:
                        if local and Path(local).exists():
                            text, meta = await self.asr.transcribe_file_meta(local)
                        elif data.get("url"):
                            text = await self.asr.transcribe_url(str(data["url"]))
                    except Exception:
                        log.exception("语音识别失败 mid=%s", message_id)
                hint = _emotion_hint(meta) if text else ""
                out.append(f"[语音：{text}]{hint}" if text else "[语音]")
                enriched = enriched or bool(text)
                continue
            out.append(segment_text(seg))
        joined = " ".join("".join(out).split())
        if enriched and joined:
            try:
                self.store.set_message_text(message_id, joined, direction="in")
            except Exception:
                log.exception("回写语音/图片文字失败 mid=%s", message_id)
        return joined

    async def _respond_locked(self, uid: int, slot: dict[str, Any]) -> None:
        name = slot["name"]
        self._pending.pop(uid, None)
        parts: list[str] = []
        for ev in slot.get("events") or []:
            piece = await self._event_text(ev)
            if piece:
                parts.append(piece)
        if not parts:
            parts = [p for p in slot["parts"] if p]
        if not parts:
            return
        user_text = "\n".join(parts)
        brain_text = user_text
        if "[语音：" in user_text:
            brain_text += "\n（这条语音已经自动转成文字给你了，你听得见他说什么。可以顺着内容接话，也可以逗他、吐槽他的声音，或者吐槽他发语音，但别装听不见。）"
        elif "[图片：" in user_text or "[表情包：" in user_text:
            brain_text += "\n（图片内容已经描述给你了，你看得到。怎么接看你当下的心情——可以聊画面细节，也可以拿它打趣，或者顺着他刚才的话说下去，别每次都同一句。）"
        t0 = time.perf_counter()
        try:
            self._capture_memory(uid, user_text)
            history = self._history(uid, user_text)
            social_text = ""
            if self.social is not None:
                acted = ""
                if getattr(self.social, "last_interact_tid", None) != user_text:
                    try:
                        acted = await self.social.interact(user_text)
                    except Exception:
                        log.exception("聊天时点赞/评论失败")
                        acted = ""
                seen = ""
                try:
                    seen = await self.social.chat_context(user_text)
                except Exception:
                    log.exception("聊天时刷空间失败")
                social_text = "\n\n".join(piece for piece in (acted, seen) if piece)
            ctx = self._context(uid, user_text, social_text=social_text)
            avoid = [
                str(item.get("content") or "")
                for item in history
                if item.get("role") == "assistant" and str(item.get("content") or "").strip()
            ][-8:]
            out = await self.brain.chat(
                user_text=brain_text,
                history=history,
                user_name=name,
                now_text=datetime.now().strftime("%Y-%m-%d %H:%M"),
                multi=settings.reply_multi,
                multi_max=settings.reply_multi_max,
                avoid_texts=avoid,
                **ctx,
            )
            bubbles = [bubble.strip() for bubble in (out.get("bubbles") or []) if bubble.strip()]
            if bubbles and avoid and _too_similar(bubbles, avoid):
                # 和她以前说过的太像，重生成一次（换个说法）
                try:
                    retry = await self.brain.chat(
                        user_text=brain_text + "\n（你刚想好的那版和从前说过的几乎一样，换一种说法，别复读。）",
                        history=history,
                        user_name=name,
                        now_text=datetime.now().strftime("%Y-%m-%d %H:%M"),
                        multi=settings.reply_multi,
                        multi_max=settings.reply_multi_max,
                        avoid_texts=avoid,
                        **ctx,
                    )
                    fresh = [bubble.strip() for bubble in (retry.get("bubbles") or []) if bubble.strip()]
                    if fresh and not _too_similar(fresh, avoid):
                        bubbles = fresh
                        self.stats["repeats_avoided"] = int(self.stats.get("repeats_avoided") or 0) + 1
                        log.info("回复与历史过于相似，已重生成一次")
                except Exception:
                    log.exception("重生成回复失败，沿用原回复")
            if not bubbles:
                raise BrainError("大脑返回空回复")
            result: dict[str, Any] = {}
            for index, bubble in enumerate(bubbles):
                if index:
                    # 分条发，像人在打字：按字数给一点间隔
                    delay = self.rng.uniform(settings.reply_bubble_delay_min, settings.reply_bubble_delay_max)
                    delay += min(1.2, len(bubble) / 60.0)
                    await asyncio.sleep(delay)
                result = await self.actions.send_text("private", uid, bubble)
                self.stats["bubbles_sent"] = int(self.stats.get("bubbles_sent") or 0) + 1
            reply = "\n".join(bubbles)
            self.stats["replied"] += 1
            self.stats["last_reply_at"] = time.time()
            self.stats["last_bubbles"] = len(bubbles)
            self.stats["last_latency_ms"] = round((time.perf_counter() - t0) * 1000)
            log.info(
                "已自动回复 user=%s latency=%sms bubbles=%s mid=%s reply=%r",
                uid,
                self.stats["last_latency_ms"],
                len(bubbles),
                result.get("message_id"),
                reply[:120],
            )
            self._spawn_reflection(uid, user_text, reply, name)
            self._spawn_sticker(uid)
            self._spawn_followup(uid, name, bubbles, ctx)
            self._spawn_feed_impulse(user_text)
        except BrainError as exc:
            self.stats["errors"] += 1
            self.stats["last_error"] = str(exc)
            log.error("自动回复失败 user=%s: %s", uid, exc)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("自动回复异常 user=%s", uid)

    def _history(self, uid: int, current_text: str) -> list[dict[str, str]]:
        rows = self.store.messages(user_id=uid, limit=settings.autoreply_history * 3)
        # 当前这条已在入库，去掉尾部重复的入站消息
        while rows and rows[-1].get("direction") == "in":
            last_text = " ".join(
                str(rows[-1].get("text") or rows[-1].get("raw_message") or "").split()
            )
            if last_text and last_text in current_text:
                rows = rows[:-1]
                continue
            break
        out: list[dict[str, str]] = []
        for row in rows[-settings.autoreply_history :]:
            content = str(row.get("text") or "")
            segs = row.get("segments")
            if not content and isinstance(segs, list) and segs:
                content = "".join(segment_text(seg) for seg in segs)
            content = " ".join(str(content).split()).strip()
            if not content:
                continue
            out.append({"role": "assistant" if row.get("direction") == "out" else "user", "content": content})
        return out

    def _capture_memory(self, uid: int, text: str) -> None:
        match = MEMORY_RE.search(text)
        if not match:
            return
        content = match.group(1).strip("。.!！~ ")
        if len(content) < 2:
            return
        try:
            memory_id = self.store.add_memory(
            content, kind="explicit", user_id=uid, persona=personas.active()
        )
            log.info("记住 user=%s id=%s: %s", uid, memory_id, content[:60])
        except Exception:
            log.exception("写入长期记忆失败")

    def _context(self, uid: int, text: str, social_text: str | None = None) -> dict[str, str]:
        """拼聊天时要用到的记忆 / 生活线 / 心情上下文。"""
        ctx = {"memory_text": "", "memo_text": "", "life_text": "", "social_text": "", "mood_text": ""}
        try:
            if self.memory is not None:
                recall = self.memory.recall(uid, text)
                ctx["memory_text"] = self.memory.format_block(recall)
                ids = [int(item["id"]) for item in recall.get("facts") or [] if item.get("id")]
                self.store.touch_memories(ids[:8])
            if self.memory is not None and getattr(self.memory, "memo_block", None):
                ctx["memo_text"] = self.memory.memo_block(uid)
            if self.life is not None:
                ctx["life_text"] = self.life.context_block()
            if social_text:
                ctx["social_text"] = social_text
            elif self.social is not None:
                try:
                    ctx["social_text"] = self.social.context_block()
                except Exception:
                    log.exception("拼社交上下文失败")
            if self.mood is not None:
                ctx["mood_text"] = self.mood.prompt_block()
        except Exception:
            log.exception("拼上下文失败，退回无记忆模式")
        return ctx

    def _spawn_reflection(self, uid: int, user_text: str, reply: str, name: str) -> None:
        if not settings.reflection_enabled or self.memory is None:
            return
        task = asyncio.create_task(self._reflect(uid, user_text, reply, name))
        self._reflect_tasks.add(task)
        task.add_done_callback(self._reflect_tasks.discard)

    async def _reflect(self, uid: int, user_text: str, reply: str, name: str) -> None:
        try:
            await self.memory.reflect(uid, user_text, reply, name=name)
            updater = getattr(self.memory, "update_memo", None)
            if updater is not None:
                await updater(uid, name=name)
        except Exception:
            log.exception("反思失败 user=%s", uid)

    # ------------------------------------------------------------ 顺手甩个表情包
    def _spawn_sticker(self, uid: int) -> None:
        if self.stickers is None:
            return
        task = asyncio.create_task(self._send_sticker(uid))
        self._reflect_tasks.add(task)
        task.add_done_callback(self._reflect_tasks.discard)

    async def _send_sticker(self, uid: int) -> None:
        try:
            await self.stickers.maybe_send(uid)
        except Exception:
            log.exception("发表情包失败 user=%s", uid)

    # ------------------------------------------------------------ 说完再补一句
    def _spawn_followup(
        self, uid: int, name: str, bubbles: list[str], ctx: dict[str, str]
    ) -> None:
        if not settings.followup_enabled or self.brain is None:
            return
        if self.rng.random() >= settings.followup_probability:
            return
        task = asyncio.create_task(self._followup(uid, name, bubbles, ctx))
        self._reflect_tasks.add(task)
        task.add_done_callback(self._reflect_tasks.discard)

    async def _followup(self, uid: int, name: str, bubbles: list[str], ctx: dict[str, str]) -> None:
        started = time.time()
        delay = self.rng.uniform(settings.followup_delay_min, settings.followup_delay_max)
        await asyncio.sleep(delay)
        # 这期间他又说话了，就别自作多情补刀
        last_in = self.store.last_message_at(user_id=uid, direction="in")
        if last_in and last_in > started:
            return
        if not bubbles:
            return
        blocks = [block for block in ctx.values() if block]
        instruction = (
            f"你刚对他说完最后一句：「{bubbles[-1]}」\n"
            "现在你又想起一句没说的，想再补一句给他。不超过 20 个字，口语，别重复刚才的意思，"
            "不要解释，不要加引号。只输出这句话。"
        )
        messages = [{"role": "system", "content": self.brain.persona()}]
        if blocks:
            messages.append({"role": "system", "content": "\n\n".join(blocks)})
        messages.append({"role": "user", "content": instruction})
        try:
            out = await self.brain.complete(messages, temperature=1.0, max_tokens=80)
        except Exception:  # noqa: BLE001
            log.exception("补话生成失败")
            return
        text = (out.get("text") or "").strip().strip("「」\"'")
        if not text or len(text) > 80:
            return
        try:
            await self.actions.send_text("private", uid, text)
            self.stats["followups"] = int(self.stats.get("followups") or 0) + 1
            log.info("补一句 user=%s text=%r", uid, text)
        except Exception:  # noqa: BLE001
            log.exception("补话发送失败")

    # ------------------------------------------------------------ 聊完顺手发空间
    def _spawn_feed_impulse(self, user_text: str) -> None:
        if self.proactive is None:
            return
        summary = " ".join(user_text.split())[:40]
        task = asyncio.create_task(self.proactive.on_chat_finished(summary=summary))
        self._reflect_tasks.add(task)
        task.add_done_callback(self._reflect_tasks.discard)

    # ------------------------------------------------------------ 手动调试
    async def simulate(self, uid: int, text: str, name: str = "") -> dict[str, Any]:
        """绕过 QQ 事件，直接走一遍「建上下文 → 调大脑 → 发送」。"""
        t0 = time.perf_counter()
        self._capture_memory(uid, text)
        history = self._history(uid, text)
        ctx = self._context(uid, text)
        out = await self.brain.chat(
            user_text=text,
            history=history,
            user_name=name or str(uid),
            now_text=datetime.now().strftime("%Y-%m-%d %H:%M"),
            multi=settings.reply_multi,
            multi_max=settings.reply_multi_max,
            **ctx,
        )
        bubbles = [bubble.strip() for bubble in (out.get("bubbles") or []) if bubble.strip()]
        if not bubbles:
            bubbles = [(out.get("text") or "").strip()]
        sent: list[dict[str, Any]] = []
        for index, bubble in enumerate(bubbles):
            if index:
                await asyncio.sleep(self.rng.uniform(settings.reply_bubble_delay_min, settings.reply_bubble_delay_max))
            sent.append(await self.actions.send_text("private", uid, bubble))
        reply = "\n".join(bubbles)
        if self.stickers is not None:
            try:
                await self.stickers.maybe_send(uid)
            except Exception:  # noqa: BLE001
                log.exception("发表情包失败 uid=%s", uid)
        self._spawn_reflection(uid, text, reply, name or str(uid))
        return {
            "reply": reply,
            "bubbles": bubbles,
            "sent": sent,
            "usage": out.get("usage"),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }
