"""高层动作：发消息 / 表情包 / 戳一戳 / 表情回应 / 撤回 / 拉历史。

所有动作都写入 messages 表（direction=out），保证「说过什么」可回溯。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import settings
from .onebot import OneBotClient, OneBotError
from .segments import (
    Segment,
    at,
    describe,
    face,
    image,
    normalize,
    reply,
    sticker_image,
    text,
)
from .store import Store

log = logging.getLogger("qqbot.tools")

TARGET_ACTIONS = {
    "group": ("send_group_msg", "group_id"),
    "private": ("send_private_msg", "user_id"),
}

_MANIFEST: dict[str, Any] | None = None
_MANIFEST_MTIME: float | None = None


def load_manifest() -> dict[str, Any]:
    global _MANIFEST, _MANIFEST_MTIME
    path = settings.sticker_dir / "manifest.json"
    if not path.exists():
        return {"items": []}
    mtime = path.stat().st_mtime
    if _MANIFEST is None or _MANIFEST_MTIME != mtime:
        try:
            _MANIFEST = json.loads(path.read_text(encoding="utf-8"))
            _MANIFEST_MTIME = mtime
        except Exception as exc:  # 配置写坏了不应该让服务起不来
            log.error("sticker manifest 解析失败: %s", exc)
            return {"items": []}
    return _MANIFEST or {"items": []}


def list_stickers(tag: str | None = None) -> list[dict[str, Any]]:
    items = [item for item in load_manifest().get("items", []) if isinstance(item, dict)]
    if not tag:
        return items
    tag = tag.lower()
    return [
        item
        for item in items
        if any(tag == str(t).lower() or tag in str(t).lower() for t in item.get("tags", []))
    ]


def pick_sticker(tag: str | None = None, sticker_id: str | None = None) -> dict[str, Any]:
    items = list_stickers() if sticker_id is None else [
        item for item in list_stickers() if str(item.get("id")) == sticker_id
    ]
    if sticker_id is not None:
        if not items:
            raise ValueError(f"没有 id={sticker_id} 的表情")
        return items[0]
    pool = list_stickers(tag) if tag else items
    if not pool:
        raise ValueError(f"表情库为空或没有 tag={tag!r} 的表情，检查 {settings.sticker_dir / 'manifest.json'}")
    return random.choice(pool)


def sticker_segment(item: dict[str, Any]) -> Segment:
    kind = item.get("kind")
    if kind == "face":
        return face(item["face_id"])
    if kind == "image":
        return sticker_image(settings.sticker_dir / str(item["file"]))
    raise ValueError(f"未知表情类型: {kind!r}")


class Actions:
    def __init__(self, ob: OneBotClient, store: Store) -> None:
        self.ob = ob
        self.store = store
        # 全局发送闸门：多人同时聊时，所有发出消息共用一条节流队列
        self._send_lock = asyncio.Lock()
        self._send_times: deque[float] = deque()
        self.sent_total = 0
        self.waited_total_ms = 0

    # ---------- 发送 ----------

    async def _guard_send(self) -> float:
        """发消息前的全局节流：相邻两条至少隔 send_spacing，每分钟不超过软上限。

        返回实际等待毫秒数。多人同时聊天时，这里排队而不是各自猛发，
        避免触发 QQ 的发送频率风控。
        """
        started = time.monotonic()
        async with self._send_lock:
            now = time.monotonic()
            while self._send_times and now - self._send_times[0] > 60.0:
                self._send_times.popleft()
            wait = 0.0
            spacing = max(0.0, settings.send_spacing)
            if self._send_times:
                wait = max(wait, self._send_times[-1] + spacing - now)
            cap = max(1, settings.send_soft_limit_per_minute)
            if len(self._send_times) >= cap:
                wait = max(wait, self._send_times[len(self._send_times) - cap] + 60.0 - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._send_times.append(time.monotonic())
            self.sent_total += 1
        elapsed = (time.monotonic() - started) * 1000
        self.waited_total_ms += int(elapsed)
        return elapsed

    async def send(
        self,
        target_type: str,
        target_id: int,
        segments: list[Any],
        *,
        reply_to: int | None = None,
    ) -> dict[str, Any]:
        target_type = target_type.lower()
        if target_type not in TARGET_ACTIONS:
            raise ValueError(f"target.type 只能是 {list(TARGET_ACTIONS)}")
        segs = normalize(segments)
        if reply_to:
            segs = [reply(reply_to), *segs]
        if not segs:
            raise ValueError("消息为空")

        action, key = TARGET_ACTIONS[target_type]
        waited = await self._guard_send()
        data = await self.ob.call(action, **{key: target_id, "message": segs})
        message_id = data.get("message_id") if isinstance(data, dict) else data

        self.store.add_message(
            time_=int(time.time()),
            message_id=message_id if isinstance(message_id, int) else None,
            message_type=target_type,
            group_id=target_id if target_type == "group" else None,
            user_id=target_id if target_type == "private" else self.ob.self_id,
            sender_name="self",
            direction="out",
            raw_message=describe(segs),
            segments=segs,
        )
        return {
            "message_id": message_id,
            "target": {"type": target_type, "id": target_id},
            "preview": describe(segs),
            "segments": segs,
        }

    async def send_text(self, target_type: str, target_id: int, content: str, *, reply_to: int | None = None) -> dict[str, Any]:
        return await self.send(target_type, target_id, [text(content)], reply_to=reply_to)

    async def send_image(self, target_type: str, target_id: int, file: str) -> dict[str, Any]:
        return await self.send(target_type, target_id, [image(file)])

    async def send_sticker(
        self,
        target_type: str,
        target_id: int,
        *,
        face_id: int | None = None,
        file: str | None = None,
        sticker_id: str | None = None,
        tag: str | None = None,
    ) -> dict[str, Any]:
        if face_id is not None:
            segs = [face(face_id)]
            picked = {"kind": "face", "face_id": face_id, "label": f"face:{face_id}"}
        elif file is not None:
            segs = [sticker_image(file)]
            picked = {"kind": "image", "file": file, "label": file}
        else:
            picked = pick_sticker(tag=tag, sticker_id=sticker_id)
            segs = [sticker_segment(picked)]
        result = await self.send(target_type, target_id, segs)
        result["sticker"] = picked
        return result

    async def poke(self, target_type: str, target_id: int, user_id: int | None = None) -> dict[str, Any]:
        if target_type == "group":
            data = await self.ob.call("group_poke", group_id=target_id, user_id=user_id or target_id)
        else:
            data = await self.ob.call("friend_poke", user_id=target_id)
        return {"ok": True, "data": data}

    async def emoji_like(self, message_id: int, emoji_id: str = "76") -> dict[str, Any]:
        """给消息贴「表情回应」（76 = 赞）。"""
        data = await self.ob.call("set_msg_emoji_like", message_id=message_id, emoji_id=str(emoji_id))
        return {"ok": True, "message_id": message_id, "emoji_id": str(emoji_id), "data": data}

    async def recall(self, message_id: int) -> dict[str, Any]:
        data = await self.ob.call("delete_msg", message_id=message_id)
        return {"ok": True, "message_id": message_id, "data": data}

    # ---------- 查询 ----------

    async def history(self, *, group_id: int | None = None, user_id: int | None = None, count: int = 20) -> Any:
        count = max(1, min(int(count), 100))
        if group_id is not None:
            return await self.ob.call("get_group_msg_history", group_id=group_id, count=count)
        if user_id is not None:
            try:
                return await self.ob.call("get_friend_msg_history", user_id=user_id, count=count)
            except OneBotError:
                return await self.ob.call("get_private_msg_history", user_id=user_id, count=count)
        raise ValueError("group_id 和 user_id 至少给一个")

    async def recall_safe(self, message_id: int) -> dict[str, Any]:
        try:
            return await self.recall(message_id)
        except OneBotError as exc:
            log.warning("撤回失败 %s: %s", message_id, exc)
            raise

    # ---------- 文字识别（NapCat 内置 OCR） ----------

    async def ocr(self, path: str | Path) -> dict[str, Any]:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"文件不存在: {p}")
        return await self.ob.call("ocr_image", image=p.as_uri())
