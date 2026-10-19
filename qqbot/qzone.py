"""QQ 空间 sidecar 客户端（对接 onebot-qzone / qzone-bridge，默认 127.0.0.1:5700）。

桥接侧是 OneBot v11 风格：POST /{action}，body 为参数，响应 {"status","retcode","data"}。
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

import httpx

from .config import ROOT, settings

log = logging.getLogger("qqbot.qzone")

MAX_IMAGE_BYTES = 5 * 1024 * 1024


class QzoneError(RuntimeError):
    pass


class QzoneClient:
    def __init__(self) -> None:
        headers = {"Content-Type": "application/json"}
        if settings.qzone_token:
            headers["Authorization"] = f"Bearer {settings.qzone_token}"
        self._http = httpx.AsyncClient(base_url=settings.qzone_base, headers=headers, timeout=90)

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, action: str, **params: Any) -> Any:
        try:
            resp = await self._http.post(f"/{action}", json=params)
        except httpx.HTTPError as exc:
            raise QzoneError(f"空间桥接不可达（{settings.qzone_base}）：{exc}") from exc
        if resp.status_code >= 400:
            raise QzoneError(f"{action} HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise QzoneError(f"{action} 返回非 JSON: {resp.text[:200]}") from exc
        if isinstance(payload, dict) and ("status" in payload or "retcode" in payload):
            if payload.get("status") == "failed":
                raise QzoneError(f"{action} 失败: {payload.get('wording') or payload.get('message') or payload}")
            return payload.get("data")
        return payload

    # ---------- 登录 / 状态 ----------

    async def login_info(self) -> Any:
        return await self.call("get_login_info")

    async def bridge_status(self) -> Any:
        return await self.call("get_status")

    async def check_cookie(self, probe: bool = True) -> Any:
        return await self.call("check_cookie", probe=probe)

    async def login_cookie(self, cookie: str) -> Any:
        return await self.call("login_cookie", cookie=cookie)

    async def login_qr(self) -> Any:
        return await self.call("login_qr")

    async def logout(self) -> Any:
        return await self.call("logout")

    # ---------- 说说 ----------

    def _cq_image(self, source: str) -> str:
        raw = source.strip()
        if raw.startswith(("http://", "https://", "base64://")):
            return f"[CQ:image,file={raw}]"
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT / raw
        if not path.exists():
            raise QzoneError(f"图片不存在: {raw}")
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise QzoneError(f"图片过大（{size / 1024 / 1024:.1f}MB）: {raw}")
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f"[CQ:image,file=base64://{b64}]"

    async def publish(self, content: str, images: list[str] | None = None) -> Any:
        message = content or ""
        for src in images or []:
            message += self._cq_image(src)
        return await self.call("send_private_msg", message=message)

    async def posts(self, *, user_id: int | None = None, pos: int = 0, num: int = 10, include_image_data: bool = False) -> Any:
        params: dict[str, Any] = {"pos": pos, "num": num, "include_image_data": include_image_data}
        if user_id:
            params["user_id"] = user_id
        return await self.call("get_emotion_list", **params)

    async def friend_feeds(self, *, num: int = 10, include_image_data: bool = False) -> Any:
        return await self.call(
            "get_friend_feeds", num=num, count=num, include_image_data=include_image_data
        )

    async def feeds_of(self, uin: int, *, count: int = 10, start: int = 0) -> Any:
        """看某个好友的空间（feed_owner 模式，等价于打开 TA 的空间页）。"""
        return await self.call(
            "get_feeds_html_act_all", feed_owner=uin, count=count, start=start
        )

    async def delete_post(self, tid: str) -> Any:
        return await self.call("delete_msg", tid=tid)

    async def set_privacy(self, tid: str, private: bool = True) -> Any:
        return await self.call("set_emotion_privacy", tid=tid, privacy="private" if private else "public")

    # ---------- 互动 ----------

    async def comment(self, *, target_uin: int, tid: str, content: str,
                      reply_comment_id: str | None = None, reply_uin: int | None = None) -> Any:
        params: dict[str, Any] = {"target_uin": target_uin, "target_tid": tid, "content": content}
        if reply_comment_id:
            params["reply_comment_id"] = reply_comment_id
        if reply_uin:
            params["reply_uin"] = reply_uin
        return await self.call("send_comment", **params)

    async def comments(self, *, user_id: int, tid: str, num: int = 20, pos: int = 0) -> Any:
        return await self.call("get_comment_list", user_id=user_id, tid=tid, num=num, pos=pos)

    async def like(self, *, tid: str, user_id: int | None = None) -> Any:
        params: dict[str, Any] = {"tid": tid}
        if user_id:
            params["user_id"] = user_id
        return await self.call("send_like", **params)

    async def unlike(self, *, tid: str, user_id: int | None = None) -> Any:
        params: dict[str, Any] = {"tid": tid}
        if user_id:
            params["user_id"] = user_id
        return await self.call("unlike", **params)

    async def like_list(self, *, user_id: int | None = None, tid: str | None = None) -> Any:
        params: dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        if tid:
            params["tid"] = tid
        return await self.call("get_like_list", **params)

    async def traffic(self, *, user_id: int, tid: str) -> Any:
        return await self.call("get_traffic_data", user_id=user_id, tid=tid)

    # ---------- 用户 / 相册 ----------

    async def portrait(self, user_id: int) -> Any:
        return await self.call("get_portrait", user_id=user_id)

    async def visitors(self, user_id: int | None = None) -> Any:
        params = {"user_id": user_id} if user_id else {}
        return await self.call("get_visitor_list", **params)

    async def albums(self, user_id: int | None = None) -> Any:
        params = {"user_id": user_id} if user_id else {}
        return await self.call("get_album_list", **params)
