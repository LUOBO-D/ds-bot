"""记忆服务：三层记忆的读写。

- 事实记忆（memories）：关于对方稳定的偏好、经历、关系；
- 情节记忆（events）：一轮对话/一天里发生的事，带情绪与显著度；
- 自我叙事（life_log）由 LifeService 负责，这里只负责在日结时引用。

两个入口：
- `recall()`：聊天前取相关记忆（关键词 + 时间衰减 + 显著度）；
- `reflect()`：聊天后异步整理这一轮（写事实、写事件、更新情绪）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from .brain import Brain, BrainError
from .config import settings
from .emotion import Mood
from .store import Store

log = logging.getLogger("qqbot.memory")


def _self_name() -> str:
    """当前角色卡的显示名（记忆整理提示词里用，避免写死名字）。"""
    from . import personas

    try:
        return personas.name(_p()) or _p()
    except Exception:  # noqa: BLE001
        return _p()


def _p() -> str:
    """当前生效角色卡 slug（记忆按角色隔离）。"""
    from . import personas

    return personas.active()

REFLECT_SYSTEM = """你是「{name}」的记忆整理器，只输出 JSON，不要输出任何多余文字。

任务：读下面这一轮对话，整理出值得长期记住的东西，并给出这一轮对心情的影响。

输出格式（JSON）：
{"facts":[{"content":"简短的事实或偏好","kind":"fact|preference|relationship|event","salience":0.1}],
 "events":[{"summary":"发生了什么，一句","emotion":"她当时的情绪一个词","salience":0.1}],
 "mood":{"valence_delta":0.0,"arousal_delta":0.0,"energy_delta":0.0,"note":"心情一句话"},
 "traits":{"closeness":0,"talkativeness":0,"sensitivity":0,"tsundere":0,"stability":0},
 "weight":0.0}

规则：
- facts 只记稳定的、以后用得上的（喜好、作息、工作、家人、约定、雷区、重要日期）；寒暄、天气、一次性的客套不要记。
- facts 最多 3 条，events 最多 2 条；没有就返回空数组。
- salience 0~1，越重要越高；日常闲聊给 0.2~0.4。
- mood 的 delta 取 -1~1：被夸/被在意为正，被冷落/吵架为负；**这一轮很平淡、没什么情绪起伏就给 0**，不要为了礼貌习惯性给正数。
- 也会给她此刻的心情（见输入）：delta 要在此基础上合理，不要每次都往上加；累、被忽略、聊得没劲都可以给负。
- traits 是这轮对她**性格倾向**的影响，每项 -3~3，日常闲聊全给 0；只有被认真对待、被冷落、吵架/和好、被当成很重要的人这类时刻才给非零（比如被陪伴 closeness +1、被敷衍 sensitivity +1、被哄好 stability +1）。
- weight 0~1：这轮的分量。日常 0；有点触动 0.3；认真交心/明显受伤/和好 0.7~1。只有 weight 高时性格基线才会真的移动，别把随口一句当大事。
- 全部用中文口语，不要出现"用户"这种词，用"他"或对方昵称。"""

NIGHTLY_SYSTEM = """你是「{name}」的记忆整理器，只输出 JSON。

任务：把今天发生的事（聊天片段 + 她自己的生活线）收束成一份日结。

输出格式：
{"summary":"今天二三句话的日结，第一人称",
 "highlights":["值得记住的瞬间，最多 3 条"],
 "facts":[{"content":"从今天能沉淀出的事实","kind":"fact|preference|relationship|event","salience":0.1}],
 "mood":{"valence_delta":0.0,"arousal_delta":0.0,"energy_delta":0.0,"note":"给今晚的心情一句话"}}

规则：summary 第一人称、具体、别写成报告；facts 最多 3 条，没有就空数组。"""

NIGHTLY_LIFE_SYSTEM = """你是「{name}」的记忆整理器，只输出 JSON。

任务：只看她今天的生活线，收束成她自己的日结。**不要提及任何具体朋友、对话内容或别人的私事**。

输出格式：
{"summary":"今天二三句话的日结，第一人称，只写她自己的生活",
 "highlights":["值得记住的瞬间，最多 3 条，只能是生活上的"],
 "facts":[{"content":"关于她自己的事实","kind":"fact|preference","salience":0.1}],
 "mood":{"valence_delta":0.0,"arousal_delta":0.0,"energy_delta":0.0,"note":"给今晚的心情一句话"}}

规则：summary 第一人称、具体、别写成报告；facts 最多 3 条，没有就空数组。"""

NIGHTLY_USER_SYSTEM = """你是「{name}」的记忆整理器，只输出 JSON。

任务：只整理「我和这一个人」在今天发生的事（对话 + 她当天生活线里相关的部分）。
**不要提及其他任何朋友**，不要写别人的私事；这份日结只给这个人看。

输出格式：
{"summary":"二三句话，对「他」说话的口吻，具体、不提别人",
 "highlights":["你们之间值得记住的瞬间，最多 3 条"],
 "facts":[{"content":"关于他或你们关系的事实","kind":"fact|preference|relationship|event","salience":0.1}]}

规则：summary 具体、别写成报告；facts 最多 3 条，没有就空数组。"""

MEMO_SYSTEM = """你是「{name}」的记忆整理器，只输出 JSON。
任务：把「今天和这个人聊到的东西」合并进一份当天备忘，方便你后面聊天时还记得这一整天。
输出格式：{"memo":"更新后的当天备忘"}
规则：
- 第一人称写，像自己随手记的小笔记；用「·」开头分行，一行一件事。
- 只记今天和这个人相关的重点：聊了什么、他的近况和情绪、约定和还没兑现的事、你答应过的事、让你在意的细节。
- 保留旧备忘里仍然有效的内容；已经过时、被推翻、或者只是一次性客套的，删掉。
- 不写别人的事，不写你自己的公开设定，不写"用户"这种词，用"他"或他的昵称。
- 最多 {max_chars} 字；宁可少写几条，也别写成流水账。"""

_JSON_RE = re.compile(r"\{.*\}", re.S)
_PUNCT = "，。！？、；：,.!?;:「」\"'“”‘’()（）~ "


def _parse_json(text: str) -> dict[str, Any]:
    match = _JSON_RE.search(text or "")
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _norm(text: str) -> str:
    return "".join(ch for ch in str(text) if ch not in _PUNCT).lower()


class MemoryService:
    def __init__(self, store: Store, brain: Brain, mood: Mood, personality: Any = None) -> None:
        self.store = store
        self.brain = brain
        self.mood = mood
        self.personality = personality
        self.stats: dict[str, Any] = {
            "reflects": 0,
            "facts": 0,
            "events": 0,
            "nightly": 0,
            "memo_updates": 0,
            "memo_skipped": 0,
            "memo_errors": 0,
            "last_reflect_at": None,
            "last_error": None,
        }

    # ------------------------------------------------------------ 检索
    def terms(self, text: str) -> list[str]:
        """中文按二元切片 + 英文/数字词，够用于 LIKE 检索。"""
        cleaned = re.sub(r"\[[^\]]{0,8}\]", " ", str(text))
        words = re.findall(r"[A-Za-z0-9_]{2,}", cleaned)
        han = re.findall(r"[\u4e00-\u9fff]{2,}", cleaned)
        grams: list[str] = []
        for chunk in han:
            if len(chunk) <= 4:
                grams.append(chunk)
                continue
            grams.extend(chunk[i : i + 2] for i in range(0, len(chunk) - 1, 2))
        return list(dict.fromkeys(words + grams))[:24]

    def recall(self, uid: int, text: str, *, k: int | None = None) -> dict[str, Any]:
        k = k or settings.memory_recall_k
        terms = self.terms(text)
        facts = self.store.search_memories(user_id=uid, terms=terms, limit=k, max_age_days=3650, persona=_p())
        k_events = max(2, int(settings.memory_events_k))
        events = self.store.events(user_id=uid, limit=max(6, k_events + 4), persona=_p())
        if terms:
            keywords = [t for t in terms if len(t) >= 2]
            scored: list[tuple[int, dict[str, Any]]] = []
            for item in events:
                summary = str(item.get("summary") or "")
                hits = sum(1 for t in keywords if t in summary)
                if hits:
                    scored.append((hits, item))
            scored.sort(key=lambda pair: pair[0], reverse=True)
            picked = [item for _, item in scored[:4]]
            if picked:
                events = picked + [e for e in events if e not in picked]
        days = self.store.day_summaries(limit=max(1, int(settings.memory_days_k)), persona=_p())
        notes = self.store.day_notes(uid, limit=max(1, int(settings.memory_notes_k)), persona=_p())
        return {"facts": facts, "events": events[:k_events], "days": days, "notes": notes}

    def format_block(self, recall: dict[str, Any]) -> str:
        lines: list[str] = []
        facts = recall.get("facts") or []
        if facts:
            lines.append("你记得关于他的事：")
            for item in facts:
                lines.append(f"- {item.get('content')}")
        events = recall.get("events") or []
        if events:
            lines.append("你们之间发生过的事（按时间从新到旧）：")
            for item in events:
                when = datetime.fromtimestamp(int(item.get("at") or 0)).strftime("%m-%d")
                lines.append(f"- {when} {item.get('summary')}")
        notes = recall.get("notes") or []
        if notes:
            lines.append("你们之间那几天的日结：")
            for item in notes[:2]:
                lines.append(f"- {item.get('day')}：{item.get('summary')}")
        days = recall.get("days") or []
        if days:
            lines.append("她最近自己的日子：")
            for item in days[:2]:
                lines.append(f"- {item.get('day')}：{item.get('summary')}")
        return "\n".join(lines)

    # ------------------------------------------------------------ 当天备忘
    @staticmethod
    def _same_day(row: dict[str, Any], day: str) -> bool:
        try:
            return datetime.fromtimestamp(float(row.get("time") or 0)).strftime("%Y-%m-%d") == day
        except Exception:  # noqa: BLE001
            return False

    def memo_block(self, uid: int) -> str:
        """当天聊天备忘（给聊天时注入用）。"""
        if not settings.memory_memo_enabled:
            return ""
        try:
            row = self.store.day_memo(uid, persona=_p())
        except Exception:  # noqa: BLE001
            return ""
        memo = str((row or {}).get("memo") or "").strip()
        if not memo:
            return ""
        return memo

    async def update_memo(self, uid: int, *, name: str = "", force: bool = False) -> dict[str, Any]:
        """把今天新聊的消息增量压进当天备忘（同人同天一条，滚动更新）。"""
        if not settings.memory_memo_enabled:
            return {"skipped": "disabled"}
        if not self.brain.ready:
            return {"skipped": "brain not ready"}
        day = datetime.now().strftime("%Y-%m-%d")
        row = self.store.day_memo(uid, day=day, persona=_p())
        last_id = int((row or {}).get("last_msg_id") or 0)
        now = time.time()
        if row and not force and now - float(row.get("updated_at") or 0) < settings.memory_memo_min_seconds:
            self.stats["memo_skipped"] += 1
            return {"skipped": "too soon"}
        start = datetime.strptime(day, "%Y-%m-%d").timestamp()
        fresh = self.store.messages(
            user_id=uid,
            after_id=last_id,
            limit=settings.memory_memo_max_msgs,
            oldest_first=True,
            since=start,
        )
        fresh = [item for item in fresh if self._same_day(item, day)]
        if not fresh:
            self.stats["memo_skipped"] += 1
            return {"skipped": "no new messages"}
        if len(fresh) < settings.memory_memo_min_msgs and not force:
            self.stats["memo_skipped"] += 1
            return {"skipped": "not enough messages"}
        lines: list[str] = []
        for item in fresh:
            speaker = "我" if item.get("direction") == "out" else "他"
            body = str(item.get("text") or "").strip()
            if not body:
                segs = item.get("segments")
                if isinstance(segs, list) and segs:
                    body = "".join(
                        str((seg.get("data") or {}).get("text") or f"[{seg.get('type')}]")
                        if str(seg.get("type")) == "text"
                        else f"[{seg.get('type')}]"
                        for seg in segs
                        if isinstance(seg, dict)
                    )
            body = " ".join(body.split())
            if body:
                lines.append(f"{speaker}：{body[:120]}")
        if not lines:
            self.stats["memo_skipped"] += 1
            return {"skipped": "no text"}
        old = str((row or {}).get("memo") or "").strip() or "（还没有，从零开始记）"
        payload = (
            f"日期：{day}\n他叫：{name or uid}\n\n"
            f"已有的当天备忘：\n{old}\n\n"
            f"今天新聊的（从旧到新）：\n" + "\n".join(lines[-80:])
        )
        system_prompt = (
            MEMO_SYSTEM.replace("{name}", _self_name())
            .replace("{max_chars}", str(int(settings.memory_memo_max_chars)))
        )
        memo = ""
        for attempt in range(2):
            try:
                out = await self.brain.complete(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": payload},
                    ],
                    temperature=0.3 if attempt == 0 else 0.2,
                    max_tokens=800,
                )
            except BrainError as exc:
                self.stats["memo_errors"] += 1
                log.warning("当天备忘更新失败：%s", exc)
                return {"error": str(exc)}
            data = _parse_json(out.get("text") or "")
            memo = str(data.get("memo") or "").strip()
            if memo:
                break
        if not memo:
            self.stats["memo_errors"] += 1
            log.warning("当天备忘压缩返回空，保留旧备忘 uid=%s", uid)
            return {"error": "empty memo", "kept": bool(old and old != "（还没有，从零开始记）")}
        if len(memo) > settings.memory_memo_max_chars:
            memo = memo[: int(settings.memory_memo_max_chars)].rstrip()
        self.store.upsert_day_memo(
            uid,
            memo,
            day=day,
            last_msg_id=int(max(int(item.get("id") or 0) for item in fresh)),
            msg_count=int((row or {}).get("msg_count") or 0) + len(fresh),
            persona=_p(),
        )
        self.stats["memo_updates"] += 1
        return {"ok": True, "messages": len(fresh), "chars": len(memo)}

    # ------------------------------------------------------------ 反思（写）
    async def reflect(
        self,
        uid: int,
        user_text: str,
        reply: str,
        *,
        name: str = "",
        message_id: int | None = None,
    ) -> dict[str, Any]:
        """一轮对话结束后异步调用：抽事实/事件 + 更新情绪。"""
        if not self.brain.ready:
            return {"skipped": "brain not ready"}
        existing = self.store.memories(user_id=uid, limit=12, persona=_p())
        known = "\n".join(f"- {item.get('content')}" for item in existing) or "（暂无）"
        mood_snap = self.mood.snapshot() if self.mood is not None else {}
        mood_line = (
            f"她此刻的心情：{mood_snap.get('label', '平常')}"
            f"（愉悦 {mood_snap.get('valence', 0)}、精力 {mood_snap.get('energy', 0)}）"
            if mood_snap
            else ""
        )
        payload = (
            f"对方（{name or uid}）说：{user_text}\n\n"
            f"{_self_name()}回答：{reply}\n\n"
            f"{mood_line}\n"
            f"已经记住的事（别重复）：\n{known}"
        )
        try:
            out = await self.brain.complete(
                [
                    {"role": "system", "content": REFLECT_SYSTEM.replace("{name}", _self_name())},
                    {"role": "user", "content": payload},
                ],
                temperature=0.3,
                max_tokens=500,
            )
        except BrainError as exc:
            self.stats["last_error"] = str(exc)
            log.warning("记忆反思失败：%s", exc)
            return {"error": str(exc)}

        data = _parse_json(out.get("text") or "")
        written: dict[str, Any] = {"facts": 0, "events": 0}
        known_norm = {_norm(item.get("content") or "") for item in existing}

        for fact in (data.get("facts") or [])[:3]:
            content = " ".join(str(fact.get("content") or "").split()).strip()
            if len(content) < 3:
                continue
            norm = _norm(content)
            if any(norm and (norm in other or other in norm) for other in known_norm if len(other) > 2):
                continue
            try:
                self.store.add_memory(
                    content,
                    kind=str(fact.get("kind") or "fact")[:32],
                    user_id=uid,
                    source="reflection",
                    salience=float(fact.get("salience") or 0.5),
                    persona=_p(),
                )
                known_norm.add(norm)
                written["facts"] += 1
            except Exception:
                log.exception("写事实记忆失败")

        for event in (data.get("events") or [])[:2]:
            summary = " ".join(str(event.get("summary") or "").split()).strip()
            if len(summary) < 4:
                continue
            try:
                self.store.add_event(
                    summary,
                    user_id=uid,
                    kind="chat",
                    emotion=str(event.get("emotion") or "")[:32] or None,
                    salience=float(event.get("salience") or 0.4),
                    source="reflection",
                    source_msg_id=message_id,
                    persona=_p(),
                )
                written["events"] += 1
            except Exception:
                log.exception("写情节记忆失败")

        mood_delta = data.get("mood") or {}
        if settings.mood_enabled and isinstance(mood_delta, dict):
            self.mood.apply_delta(mood_delta, note=str(mood_delta.get("note") or ""))

        if self.personality is not None and isinstance(data.get("traits"), dict):
            try:
                note = (mood_delta.get("note") if isinstance(mood_delta, dict) else "") or ""
                if not note and isinstance(data.get("events"), list) and data["events"]:
                    note = str((data["events"][0] or {}).get("summary") or "")
                written["traits"] = self.personality.nudge(
                    data.get("traits") or {},
                    source="reflect",
                    note=str(note)[:200],
                    weight=float(data.get("weight") or 0.0),
                )
            except Exception:  # noqa: BLE001
                log.exception("性格结算失败")

        self.stats["reflects"] += 1
        self.stats["facts"] += written["facts"]
        self.stats["events"] += written["events"]
        self.stats["last_reflect_at"] = time.time()
        log.info(
            "反思完成 user=%s facts=%s events=%s mood=%s",
            uid,
            written["facts"],
            written["events"],
            (mood_delta.get("note") or "")[:40] if isinstance(mood_delta, dict) else "",
        )
        return {"written": written, "mood": self.mood.snapshot()}

    # ------------------------------------------------------------ 日结
    async def nightly(self, day: str, *, uid: int | None = None) -> dict[str, Any]:
        """日结。uid=None 只写她自己的生活（共享）；uid=某人 只写和那个人的部分（私有）。"""
        if not self.brain.ready:
            return {"skipped": "brain not ready"}
        since = int(datetime.strptime(day, "%Y-%m-%d").timestamp())
        until = since + 86400
        lines: list[str] = []
        if uid is not None:
            rows = [
                row
                for row in self.store.messages(user_id=uid, limit=400)
                if since <= int(row.get("time") or 0) < until
            ]
            for row in rows[-120:]:
                speaker = "他" if row.get("direction") == "in" else "我"
                content = row.get("raw_message") or ""
                segs = row.get("segments")
                if isinstance(segs, list) and segs:
                    content = "".join(
                        str((seg.get("data") or {}).get("text") or f"[{seg.get('type')}]")
                        if str(seg.get("type")) == "text"
                        else f"[{seg.get('type')}]"
                        for seg in segs
                        if isinstance(seg, dict)
                    )
                content = " ".join(str(content).split()).strip()
                if content:
                    lines.append(f"{speaker}：{content[:120]}")
            if not lines:
                return {"skipped": "no chat with this user"}
        life = self.store.life_log(day=day, limit=30, persona=_p())
        life_lines = "\n".join(f"- {item.get('kind')}：{item.get('content')}" for item in life)
        if not lines and not life_lines:
            return {"skipped": "today empty"}
        mood_snap = self.mood.snapshot()
        if uid is None:
            system = NIGHTLY_LIFE_SYSTEM.replace("{name}", _self_name())
            payload = (
                f"日期：{day}\n她今天的心情：{mood_snap['label']}\n\n"
                f"她今天的生活线：\n{life_lines or '（无）'}"
            )
        else:
            system = NIGHTLY_USER_SYSTEM.replace("{name}", _self_name())
            payload = (
                f"日期：{day}\n她今天的心情：{mood_snap['label']}\n\n"
                f"和这个人的对话：\n" + "\n".join(lines[-60:])
                + f"\n\n她今天的生活线（只挑与他相关的）：\n{life_lines or '（无）'}"
                + (
                    f"\n\n今天的对话备忘（已经压缩过，直接用来写日结）：\n{memo_row['memo']}"
                    if (memo_row := self.store.day_memo(uid, day=day, persona=_p()))
                    else ""
                )
            )
        try:
            out = await self.brain.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": payload},
                ],
                temperature=0.5,
                max_tokens=600,
            )
        except BrainError as exc:
            self.stats["last_error"] = str(exc)
            log.warning("日结失败：%s", exc)
            return {"error": str(exc)}

        data = _parse_json(out.get("text") or "")
        summary = " ".join(str(data.get("summary") or "").split()).strip()
        if not summary:
            return {"error": "日结为空"}
        if uid is None:
            self.store.upsert_day_summary(
                day,
                summary,
                mood=self.mood.label(),
                highlights=data.get("highlights") or [],
                persona=_p(),
            )
            if settings.mood_enabled and isinstance(data.get("mood"), dict):
                self.mood.apply_delta(data["mood"], note=str(data["mood"].get("note") or ""), scale=0.6)
        else:
            self.store.upsert_day_note(
                day,
                int(uid),
                summary,
                mood=self.mood.label(),
                highlights=data.get("highlights") or [],
                persona=_p(),
            )
        for fact in (data.get("facts") or [])[:3]:
            content = " ".join(str(fact.get("content") or "").split()).strip()
            if len(content) < 3:
                continue
            try:
                self.store.add_memory(
                    content,
                    kind=str(fact.get("kind") or "fact")[:32],
                    user_id=uid,
                    source="nightly",
                    salience=float(fact.get("salience") or 0.5),
                    persona=_p(),
                )
            except Exception:
                log.exception("日结写记忆失败")
        self.stats["nightly"] += 1
        log.info("日结完成 day=%s uid=%s summary=%r", day, uid, summary[:60])
        return {"day": day, "uid": uid, "summary": summary}
