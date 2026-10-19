"""她的社交生活：刷一刷好友的空间，看到的东西会进记忆和聊天上下文。

- 定时（心跳）按概率去刷；也有最小间隔和每日上限，避免像爬虫。
- 刷到的说说落库（qzone_feeds），按 tid 去重。
- 刷完之后写一条生活线，让她「真的看过」，聊天时能自然提起。
- 互动：可以给好友的说说点赞、写一句评论；聊天里说「点个赞 / 评论一下」就当场做，
  心跳刷到新说说时也会自己偶尔点个赞（可关）。
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from typing import Any

from . import personas
from .config import settings
from .store import Store

log = logging.getLogger("qqbot.social")

# 一句话里出现这些词，就当作「她想看看朋友的空间」
SPACE_WORDS = ("空间", "说说", "动态", "朋友圈", "qzone")
LIKE_WORDS = ("点赞", "点个赞", "赞一下", "给个赞", "点个喜欢", "点一下赞")
COMMENT_WORDS = ("评论", "评一句", "评一下", "说一句", "回一句", "留个言", "留言")


def wants_space(text: str) -> bool:
    low = str(text or "").lower()
    return any(word.lower() in low for word in SPACE_WORDS)


def wants_like(text: str) -> bool:
    raw = str(text or "")
    return any(word in raw for word in LIKE_WORDS)


def wants_comment(text: str) -> bool:
    raw = str(text or "")
    return any(word in raw for word in COMMENT_WORDS)


JUDGE_RULES = (
    "你在刷朋友的空间，看到一条说说。先像真实的人一样判断自己喜不喜欢它，再决定要不要互动。"
    "不要客气、不要为了礼貌而点赞。只输出四行，不要解释、不要多余文字：\n"
    "兴趣：0 到 1 之间的小数（越喜欢越高）\n"
    "点赞：想点赞写 yes，不想写 no\n"
    "评论：想评论就写一句不超过 20 字的评论正文，不想就写 no\n"
    "感觉：一句话说你的感受（不超过 20 字）"
)

NO_WORDS = ("no", "否", "不", "无", "没", "算了", "跳过")

COMMENT_RULES = (
    "你要给朋友的说说写一句评论：不超过 15 个字，口语，像随手留一句，"
    "不说教、不总结、不用表情符号、不加引号和解释，直接给出评论正文。"
)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _hour() -> int:
    return datetime.now().hour


def _clip(text: Any, limit: int = 60) -> str:
    raw = " ".join(str(text or "").split())
    if "[图片" in raw or "CQ:image" in raw:
        raw = raw.split("[图片")[0].strip()
    return raw[:limit]


class Social:
    """刷好友空间 + 把看到的东西接到对话上下文里。"""

    def __init__(
        self,
        store: Store,
        qzone: Any,
        life: Any | None = None,
        mood: Any | None = None,
        *,
        self_id: int = 0,
        onebot: Any | None = None,
        brain: Any | None = None,
    ) -> None:
        self.store = store
        self.qzone = qzone
        self.life = life
        self.mood = mood
        self.self_id = int(self_id or 0)
        self.onebot = onebot
        self.brain = brain
        self.last_interact_tid: str | None = None
        self._friend_map: dict[str, int] = {}
        self._friend_map_at = 0.0
        self.stats: dict[str, Any] = {
            "browses": 0,
            "browsed": 0,
            "new_feeds": 0,
            "likes": 0,
            "comments": 0,
            "skipped": 0,
            "errors": 0,
            "last_browse_at": None,
            "last_error": None,
        }

    # ------------------------------------------------------------ kv 计数
    def _roll_day(self) -> None:
        if self.store.get_kv("social.day") != _today():
            self.store.set_kv("social.day", _today())
            self.store.set_kv("social.browses", "0")

    def _int_kv(self, key: str) -> int:
        try:
            return int(self.store.get_kv(key) or 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------ 状态
    def status(self) -> dict[str, Any]:
        self._roll_day()
        last = self._int_kv("social.last_browse_at")
        return {
            "enabled": settings.social_enabled,
            "qzone": bool(settings.qzone_base),
            "browses_today": self._int_kv("social.browses"),
            "daily_max": settings.social_browse_daily_max,
            "interval_seconds": settings.social_browse_interval_seconds,
            "window": [settings.social_window_start, settings.social_window_end],
            "last_browse_at": last or None,
            "last_browse_ago": (int(time.time()) - last) if last else None,
            "stats": self.stats,
        }

    def _browse_block_reason(self, *, force: bool) -> str | None:
        if not settings.social_enabled and not force:
            return "社交线已关闭"
        self._roll_day()
        if not force:
            hour = _hour()
            if not (settings.social_window_start <= hour < settings.social_window_end):
                return f"现在 {hour} 点，不是刷空间的时段"
            if self._int_kv("social.browses") >= settings.social_browse_daily_max:
                return "今天刷得够多了"
            last = self._int_kv("social.last_browse_at")
            if last and time.time() - last < settings.social_browse_interval_seconds:
                return "刚刷过，过一会儿再刷"
        return None

    # ------------------------------------------------------------ 刷空间
    async def browse(self, *, num: int = 8, force: bool = False) -> dict[str, Any]:
        reason = self._browse_block_reason(force=force)
        if reason:
            self.stats["skipped"] += 1
            return {"ok": False, "skipped": reason}
        try:
            data = await self.qzone.friend_feeds(num=num)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("刷好友空间失败")
            return {"ok": False, "error": repr(exc)}

        raw = data.get("msglist") if isinstance(data, dict) else data
        items = raw if isinstance(raw, list) else []
        new_items: list[dict[str, Any]] = []
        skipped_own = 0
        self_id = int(getattr(self.onebot, "self_id", 0) or self.self_id or 0)
        for item in items[:num]:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("tid") or "")
            if not tid:
                continue
            if self_id and int(item.get("uin") or 0) == self_id:
                skipped_own += 1
                continue
            exists = self.store.feed_by_tid(tid)
            content = _clip(item.get("content"), 300)
            if exists:
                continue
            feed_id = self.store.add_feed(
                tid=tid,
                uin=int(item.get("uin") or 0) or None,
                nickname=str(item.get("nickname") or "").strip()[:40],
                content=content,
                created_time=int(item.get("created_time") or 0) or None,
                comment_count=int(item.get("cmtnum") or 0),
                like_count=int(item.get("likenum") or 0),
                pic_count=len(item.get("pic") or []),
                persona=personas.active(),
            )
            new_items.append({"id": feed_id, "tid": tid, "nickname": item.get("nickname"), "content": content})

        now = int(time.time())
        self.store.set_kv("social.last_browse_at", str(now))
        self.store.set_kv("social.browses", str(self._int_kv("social.browses") + 1))
        self.stats["browses"] += 1
        self.stats["browsed"] += len(items)
        self.stats["new_feeds"] += len(new_items)
        self.stats["last_browse_at"] = now

        if new_items:
            await self._record_life(new_items)
            await self._autonomy(new_items)
        log.info("刷空间：看到 %s 条，新 %s 条（自己 %s 条）", len(items), len(new_items), skipped_own)
        return {
            "ok": True,
            "seen": len(items),
            "new": len(new_items),
            "own": skipped_own,
            "items": new_items[:5],
        }

    async def _record_life(self, items: list[dict[str, Any]]) -> None:
        if self.life is None or not items:
            return
        parts = [f"{item.get('nickname') or '有人'}：{_clip(item.get('content'), 40)}" for item in items[:2]]
        text = "刷了会儿空间。" + "；".join(parts)
        try:
            self.store.add_life(
                text,
                day=_today(),
                kind="social",
                tags=["qzone", "browse"],
                persona=personas.active(),
            )
        except Exception:  # noqa: BLE001
            log.exception("写生活线失败（刷空间）")

    def _ingest(self, items: list[Any], *, limit: int) -> tuple[int, int, list[dict[str, Any]]]:
        """把 msglist 落库；返回 (看到条数, 自己条数, 新增列表)。"""
        new_items: list[dict[str, Any]] = []
        skipped_own = 0
        self_id = int(getattr(self.onebot, "self_id", 0) or self.self_id or 0)
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            tid = str(item.get("tid") or item.get("detailTid") or "")
            if not tid:
                continue
            if self_id and int(item.get("uin") or 0) == self_id:
                skipped_own += 1
                continue
            if self.store.feed_by_tid(tid):
                continue
            content = _clip(item.get("content"), 300)
            feed_id = self.store.add_feed(
                tid=tid,
                uin=int(item.get("uin") or 0) or None,
                nickname=str(item.get("nickname") or "").strip()[:40],
                content=content,
                created_time=int(item.get("created_time") or 0) or None,
                comment_count=int(item.get("cmtnum") or 0),
                like_count=int(item.get("likenum") or 0),
                pic_count=len(item.get("pic") or []),
                persona=personas.active(),
            )
            new_items.append(
                {"id": feed_id, "tid": tid, "nickname": item.get("nickname"), "content": content}
            )
        return len(items), skipped_own, new_items

    async def friend_map(self) -> dict[str, int]:
        """昵称/备注 -> QQ 号，用来把「小萝卜的空间」翻成 uin。"""
        now = time.time()
        if self._friend_map and now - self._friend_map_at < 600:
            return self._friend_map
        if self.onebot is None:
            return self._friend_map
        try:
            data = await self.onebot.get_friend_list()
        except Exception:  # noqa: BLE001
            log.exception("拉好友列表失败")
            return self._friend_map
        self_id = int(getattr(self.onebot, "self_id", 0) or 0)
        mapping: dict[str, int] = {}
        for row in data if isinstance(data, list) else []:
            if not isinstance(row, dict):
                continue
            try:
                uid = int(row.get("user_id") or row.get("uin") or 0)
            except (TypeError, ValueError):
                continue
            if not uid or (self_id and uid == self_id):
                continue
            for key in ("remark", "nickname", "card"):
                name = str(row.get(key) or "").strip()
                if name:
                    mapping[name] = uid
        self._friend_map, self._friend_map_at = mapping, now
        return mapping

    async def resolve_friend(self, text: str) -> dict[str, Any] | None:
        mapping = await self.friend_map()
        hits = {name: uid for name, uid in mapping.items() if name and name in str(text or "")}
        if not hits:
            return None
        name = max(hits, key=len)
        return {"name": name, "uin": hits[name]}

    async def view_friend(self, uin: int, *, count: int = 10) -> dict[str, Any]:
        """看某个好友的空间首页（feed_owner 模式），结果同样落库。"""
        try:
            data = await self.qzone.feeds_of(uin, count=count)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("看好友空间失败 uin=%s", uin)
            return {"ok": False, "error": repr(exc)}
        payload = data if isinstance(data, dict) else {}
        raw = payload.get("msglist") or payload.get("posts") or payload.get("list")
        items = raw if isinstance(raw, list) else []
        if not items and isinstance(data, list):
            items = data
        seen, own, new_items = self._ingest(items, limit=max(1, count))
        if new_items:
            await self._record_life(new_items)
        log.info("看空间 uin=%s：%s 条，新 %s 条", uin, seen, len(new_items))
        return {"ok": True, "uin": uin, "seen": seen, "new": len(new_items), "items": new_items[:5]}

    async def chat_context(self, user_text: str, limit: int = 5) -> str:
        """聊天时先确保「她刚看过」：提到空间就现刷一次（带冷却）。"""
        if not settings.social_enabled:
            return ""
        focus: dict[str, Any] | None = None
        if wants_space(user_text):
            await self._chat_refresh(user_text)
            focus = await self.resolve_friend(user_text)
        return self.context_block(
            limit=limit,
            focus_uin=int(focus["uin"]) if focus else None,
            focus_name=str(focus["name"]) if focus else "",
        )

    async def _chat_refresh(self, text: str) -> None:
        now = time.time()
        last = self._int_kv("social.chat_at")
        if last and now - last < settings.social_chat_cooldown_seconds:
            return
        self.store.set_kv("social.chat_at", str(int(now)))
        friend = await self.resolve_friend(text)
        if friend:
            result = await self.view_friend(int(friend["uin"]))
            log.info("聊天触发看空间：%s(%s) new=%s", friend["name"], friend["uin"], result.get("new"))
            return
        result = await self.browse(force=True)
        log.info("聊天触发刷好友动态：new=%s", result.get("new"))

    # ------------------------------------------------------------ 互动（点赞 / 评论）
    def _cached_verdict(self, feed: dict[str, Any]) -> dict[str, Any] | None:
        if feed.get("verdict_at") and feed.get("interest") is not None:
            interest = float(feed["interest"])
            return {
                "interest": interest,
                "like": interest >= settings.social_judge_min_interest,
                "comment_text": str(feed.get("comment_text") or ""),
                "reason": str(feed.get("verdict_reason") or ""),
                "cached": True,
            }
        return None

    async def judge(self, feed: dict[str, Any], *, force: bool = False) -> dict[str, Any]:
        """问她本人：这条说说你喜不喜欢、想不想点赞/评论。"""
        if not settings.social_judge_enabled:
            return {"interest": 0.6, "like": True, "comment_text": "", "reason": "", "cached": False}
        if not force:
            cached = self._cached_verdict(feed)
            if cached:
                return cached
        if self.brain is None or not getattr(self.brain, "ready", False):
            return {"interest": 0.5, "like": False, "comment_text": "", "reason": "", "cached": False}
        who = feed.get("nickname") or "朋友"
        content = _clip(feed.get("content"), 150) or "（只发了图片，没写文字）"
        messages = [
            {"role": "system", "content": personas.text() + "\n\n" + JUDGE_RULES},
            {"role": "user", "content": f"{who} 的说说：{content}\n按格式给出你的判断。"},
        ]
        try:
            result = await self.brain.complete(messages, temperature=0.6, max_tokens=120)
        except Exception:  # noqa: BLE001
            log.exception("判断说说喜好失败")
            return {"interest": 0.5, "like": False, "comment_text": "", "reason": "", "cached": False}
        text = str(result.get("text") or "")
        interest = None
        match = re.search(r"(?:兴趣|喜欢)[:：]\s*([0-9.]+)", text)
        if match:
            try:
                interest = max(0.0, min(1.0, float(match.group(1))))
            except ValueError:
                interest = None
        like_raw = ""
        match = re.search(r"点赞[:：]\s*(.+)", text)
        if match:
            like_raw = match.group(1).strip().lower()
        like = interest is not None and interest >= settings.social_judge_min_interest
        if like_raw:
            like = not any(word in like_raw for word in NO_WORDS)
            if interest is not None and interest < settings.social_judge_min_interest and "yes" in like_raw:
                like = False
        comment_text = ""
        match = re.search(r"评论[:：]\s*(.+)", text)
        if match:
            candidate = match.group(1).strip().strip('"“”')
            if candidate and not any(candidate.lower().startswith(word) for word in NO_WORDS):
                comment_text = " ".join(candidate.split())[:20]
        reason = ""
        match = re.search(r"感觉[:：]\s*(.+)", text)
        if match:
            reason = " ".join(match.group(1).split())[:40]
        verdict = {
            "interest": interest if interest is not None else (0.7 if like else 0.3),
            "like": bool(like),
            "comment_text": comment_text,
            "reason": reason,
            "cached": False,
        }
        try:
            self.store.set_verdict(
                str(feed.get("tid") or ""),
                interest=verdict["interest"],
                like=verdict["like"],
                comment_text=comment_text,
                reason=reason,
            )
        except Exception:  # noqa: BLE001
            log.exception("写判定失败")
        log.info("判定 tid=%s interest=%.2f like=%s comment=%r", feed.get("tid"), verdict["interest"], verdict["like"], comment_text)
        return verdict

    async def like_feed(self, feed: dict[str, Any]) -> dict[str, Any]:
        tid = str(feed.get("tid") or "")
        if not tid:
            return {"ok": False, "error": "没有说说 id"}
        uin = int(feed.get("uin") or 0) or None
        try:
            await self.qzone.like(tid=tid, user_id=uin)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("点赞失败 tid=%s", tid)
            return {"ok": False, "error": repr(exc)}
        self.store.mark_feed(tid, liked=True)
        self.stats["likes"] += 1
        self._note(f"给{feed.get('nickname') or '他'}的说说点了赞：" + _clip(feed.get("content"), 20))
        log.info("点赞 ok tid=%s", tid)
        return {"ok": True, "tid": tid}

    async def comment_feed(self, feed: dict[str, Any], content: str) -> dict[str, Any]:
        tid = str(feed.get("tid") or "")
        uin = int(feed.get("uin") or 0)
        text = " ".join(str(content or "").split())[:60]
        if not tid or not uin or not text:
            return {"ok": False, "error": "参数不全"}
        try:
            await self.qzone.comment(target_uin=uin, tid=tid, content=text)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.exception("评论失败 tid=%s", tid)
            return {"ok": False, "error": repr(exc)}
        self.store.mark_feed(tid, comment_text=text)
        self.stats["comments"] += 1
        self._note(f"在{feed.get('nickname') or '他'}的说说下评论了：{text}")
        log.info("评论 ok tid=%s content=%s", tid, text)
        return {"ok": True, "tid": tid, "content": text}

    def _note(self, text: str) -> None:
        if self.life is None:
            return
        try:
            self.store.add_life(
                text,
                day=_today(),
                kind="social",
                tags=["qzone", "interact"],
                persona=personas.active(),
            )
        except Exception:  # noqa: BLE001
            log.exception("写生活线失败（互动）")

    async def _think_comment(self, feed: dict[str, Any]) -> str:
        if self.brain is None or not getattr(self.brain, "ready", False):
            return ""
        who = feed.get("nickname") or "朋友"
        content = _clip(feed.get("content"), 120) or "（只发了图片）"
        messages = [
            {"role": "system", "content": personas.text() + "\n\n" + COMMENT_RULES},
            {"role": "user", "content": f"{who} 的说说：{content}\n写一句评论。"},
        ]
        try:
            result = await self.brain.complete(messages, temperature=0.9, max_tokens=60)
        except Exception:  # noqa: BLE001
            log.exception("生成评论失败")
            return ""
        text = str(result.get("text") or "").strip().strip('"“”')
        return " ".join(text.split())[:20]

    async def _latest_feed_of(self, *, uin: int | None = None) -> dict[str, Any] | None:
        rows = self.store.feeds(limit=12, persona=personas.active())
        self_id = int(getattr(self.onebot, "self_id", 0) or self.self_id or 0)
        for row in rows:
            if self_id and int(row.get("uin") or 0) == self_id:
                continue
            if uin and int(row.get("uin") or 0) != int(uin):
                continue
            return row
        return None

    async def interact(self, user_text: str) -> str:
        """聊天里说「点个赞 / 评论一下」时：先按她自己的喜好判断，再决定做不做。"""
        if not settings.social_enabled:
            return ""
        like = wants_like(user_text)
        comment = wants_comment(user_text)
        if not (like or comment):
            return ""
        self.last_interact_tid = str(user_text or "")[:200]
        friend = await self.resolve_friend(user_text)
        feed: dict[str, Any] | None = None
        if friend:
            await self.view_friend(int(friend["uin"]), count=5)
            feed = await self._latest_feed_of(uin=int(friend["uin"]))
        if feed is None:
            feed = await self._latest_feed_of()
        if feed is None:
            return "你想给朋友的说说点个赞，但最近没刷到可以互动的说说。"
        verdict = await self.judge(feed)
        who = feed.get("nickname") or "他"
        notes: list[str] = []
        if like:
            if verdict["like"]:
                result = await self.like_feed(feed)
                if result.get("ok"):
                    notes.append(f"你给{who}的说说点了赞（「{_clip(feed.get('content'), 20)}」）")
                else:
                    notes.append("点赞没成功")
            else:
                feeling = verdict.get("reason") or "没什么感觉"
                notes.append(f"你看了{who}的说说，但没点赞，因为你不喜欢这条（{feeling}）")
        if comment:
            if verdict["like"]:
                text = verdict.get("comment_text") or await self._think_comment(feed)
                if text:
                    result = await self.comment_feed(feed, text)
                    if result.get("ok"):
                        notes.append(f"你在这条说说下评论了：「{text}」")
                else:
                    notes.append("你想评论，但没想好说什么")
            else:
                notes.append("你也没评论——不喜欢的东西不想硬夸")
        if not notes:
            return ""
        return (
            "你刚刚做过的事（结合你真实的喜好，别再说「我去点赞 / 我去评论」，直接说结果和感受）：\n"
            + "\n".join(notes)
        )

    async def _autonomy(self, new_items: list[dict[str, Any]]) -> None:
        """心跳刷到新说说时，按她自己的喜好决定要不要点赞 / 评论。"""
        owner = int(settings.owner_uid or 0)
        for item in new_items:
            feed = self.store.feed_by_tid(str(item.get("tid") or ""))
            if not feed or feed.get("liked"):
                continue
            verdict = await self.judge(feed)
            if verdict["like"] and random.random() < settings.social_auto_like_probability:
                await self.like_feed(feed)
            if (
                owner
                and int(feed.get("uin") or 0) == owner
                and verdict["like"]
                and verdict.get("comment_text")
                and random.random() < settings.social_auto_comment_probability
            ):
                await self.comment_feed(feed, verdict["comment_text"])

    # ------------------------------------------------------------ 上下文
    def context_block(
        self,
        limit: int = 3,
        *,
        focus_uin: int | None = None,
        focus_name: str = "",
    ) -> str:
        """把最近刷到的动态拼成一小块，给聊天用；没有就返回空串。"""
        if not settings.social_enabled:
            return ""
        self_id = int(getattr(self.onebot, "self_id", 0) or self.self_id or 0)
        rows = self.store.feeds(limit=limit + 4, persona=personas.active())
        feeds = [row for row in rows if not (self_id and int(row.get("uin") or 0) == self_id)]
        if not feeds:
            return ""
        focus_lines: list[str] = []
        other_lines: list[str] = []
        for feed in feeds:
            who = feed.get("nickname") or "有人"
            text = _clip(feed.get("content"), 80)
            if not text:
                continue
            line = f"- {who}：{text}"
            if focus_uin and int(feed.get("uin") or 0) == int(focus_uin):
                focus_lines.append(line)
            else:
                other_lines.append(line)
        parts: list[str] = []
        if focus_lines:
            who = focus_name or "TA"
            parts.append(
                f"你刚打开 {who} 的空间，看到的最新说说（现在就能直接说给他听，不用再说我去看看）：\n"
                + "\n".join(focus_lines[:3])
            )
        if other_lines:
            parts.append("你最近刷到的其他朋友动态：\n" + "\n".join(other_lines[:limit]))
        if focus_uin and not focus_lines:
            parts.append(
                f"你刚打开 {focus_name or 'TA'} 的空间，没看到新说说。"
            )
        joined = "\n".join(parts)
        if "阿鱼" in joined:
            joined += "\n\n（「阿鱼」是你的 QQ 昵称，朋友说的就是你，别问那是谁。）"
        return joined

    # ------------------------------------------------------------ 心跳
    async def tick(self, *, force: bool = False) -> dict[str, Any] | None:
        if not settings.social_enabled and not force:
            return None
        if not force and not self._browse_due():
            return None
        return await self.browse(force=force)

    def _browse_due(self) -> bool:
        self._roll_day()
        hour = _hour()
        if not (settings.social_window_start <= hour < settings.social_window_end):
            return False
        if self._int_kv("social.browses") >= settings.social_browse_daily_max:
            return False
        last = self._int_kv("social.last_browse_at")
        if last and time.time() - last < settings.social_browse_interval_seconds:
            return False
        # 15 分钟一拍，不是每拍都去刷
        return random.random() < settings.social_browse_probability
