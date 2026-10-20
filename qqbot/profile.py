"""她自己的 QQ 个人主页：个性签名、头像、资料。

- 签名：由她结合人设 / 当天生活 / 心情自己写，每天最多换一次
- 头像：默认用你的设定图，之后可以换她自己觉得好看的
- 全部动作写 audit，可开关
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .config import settings

log = logging.getLogger("qqbot.profile")

SIGNATURE_PROMPT = """你在维护自己的 QQ 个性签名，就像一个普通女孩会做的那样。

要求：
- 一句话，最多 {max_chars} 个字，不加引号、不加书名号、不用 emoji 堆砌
- 写此刻真实的状态或一句轻的话，别写自我介绍、别写格言、别喊口号
- 和上一次的签名不能雷同；可以带一点今天的生活细节
- 只输出签名正文，不要解释

人设卡（节选你的口吻）：{persona}

今天你过的日子：{life}
你现在的心情：{mood}
上一次的签名：{last}
"""


class ProfileService:
    def __init__(self, store, actions, *, brain=None, mood=None, life=None) -> None:
        self.store = store
        self.actions = actions
        self.brain = brain
        self.mood = mood
        self.life = life
        self.stats = {
            "signatures": 0,
            "skipped": 0,
            "errors": 0,
            "last_error": None,
            "last_signature": None,
            "last_at": None,
        }

    # ---------------------------------------------------------------- 查询
    async def current(self) -> dict[str, Any]:
        info: dict[str, Any] = {}
        try:
            raw = await self.actions.ob.call("get_stranger_info", user_id=self.actions.ob.self_id)
            info = raw.get("data") if isinstance(raw, dict) and "data" in raw else (raw or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("读取个人资料失败：%r", exc)
        return {
            "self_id": self.actions.ob.self_id,
            "nickname": (info or {}).get("nickname") or "",
            "long_nick": (info or {}).get("longNick") or (info or {}).get("long_nick") or "",
            "raw": info,
            "local": {
                "signature": self.store.get_kv("profile.signature") or "",
                "avatar_path": self.store.get_kv("profile.avatar_path") or "",
                "last_sig_at": self.store.get_kv("profile.last_sig_at"),
                "last_avatar_at": self.store.get_kv("profile.last_avatar_at"),
            },
            "stats": dict(self.stats),
        }

    # ---------------------------------------------------------------- 签名
    def _last_signature(self) -> str:
        return str(self.store.get_kv("profile.signature") or "")

    async def compose_signature(self) -> str:
        if self.brain is None:
            return ""
        day = datetime.now().strftime("%Y-%m-%d")
        life_text = ""
        if self.life is not None:
            try:
                life_text = self.life.context_block(day, limit=2)
            except Exception:  # noqa: BLE001
                life_text = ""
        mood_text = ""
        if self.mood is not None:
            try:
                mood_text = self.mood.prompt_block()
            except Exception:  # noqa: BLE001
                mood_text = ""
        persona = self.brain.persona()
        prompt = SIGNATURE_PROMPT.replace("{max_chars}", str(int(settings.profile_signature_max)))
        prompt = prompt.replace("{persona}", persona[:600])
        prompt = prompt.replace("{life}", life_text[:400] or "（今天还没什么特别的）")
        prompt = prompt.replace("{mood}", mood_text[:200] or "（平平常常）")
        prompt = prompt.replace("{last}", self._last_signature() or "（还没有，第一次写）")
        try:
            out = await self.brain.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.9,
                max_tokens=80,
            )
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            log.warning("写签名失败：%r", exc)
            return ""
        text = " ".join(str(out.get("text") or "").split())
        text = text.strip("「」\"'“”‘’ \n")
        if len(text) > int(settings.profile_signature_max):
            text = text[: int(settings.profile_signature_max)]
        return text

    async def set_signature(self, text: str, *, actor: str = "api") -> dict[str, Any]:
        text = " ".join(str(text or "").split())[: int(settings.profile_signature_max)]
        if not text:
            return {"ok": False, "error": "empty"}
        try:
            await self.actions.ob.call("set_self_longnick", longNick=text)
        except Exception as exc:  # noqa: BLE001
            self.stats["errors"] += 1
            self.stats["last_error"] = repr(exc)
            self.store.audit(actor, "profile.signature", {"text": text}, {"error": repr(exc)}, False)
            return {"ok": False, "error": repr(exc)}
        self.store.set_kv("profile.signature", text)
        self.store.set_kv("profile.last_sig_at", int(time.time()))
        self.stats["signatures"] += 1
        self.stats["last_signature"] = text
        self.stats["last_at"] = int(time.time())
        self.store.audit(actor, "profile.signature", {"text": text}, {"ok": True}, True)
        log.info("个性签名已更新：%s", text)
        return {"ok": True, "text": text}

    # ---------------------------------------------------------------- 自动维护
    async def tick(self, *, force: bool = False) -> dict[str, Any]:
        if not getattr(settings, "profile_enabled", True):
            return {"skipped": "disabled"}
        if not getattr(self.actions.ob, "connected", True):
            return {"skipped": "gateway offline"}
        now = time.time()
        last = float(self.store.get_kv("profile.last_sig_at") or 0)
        gap = settings.profile_signature_hours * 3600
        if not force and now - last < gap:
            self.stats["skipped"] += 1
            return {"skipped": "too soon"}
        text = await self.compose_signature()
        if not text:
            return {"skipped": "empty"}
        if text == self._last_signature():
            self.stats["skipped"] += 1
            return {"skipped": "same"}
        return await self.set_signature(text, actor="keeper")

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(getattr(settings, "profile_enabled", True)),
            "signature_hours": settings.profile_signature_hours,
            "signature_max": settings.profile_signature_max,
            "avatar_readonly": self.store.get_kv("profile.avatar_path") or "",
            "signature": self.store.get_kv("profile.signature") or "",
            "stats": dict(self.stats),
        }
