"""OneBot 11 网关客户端（反向 WebSocket 为主，HTTP 兜底）。

NapCat 作为 WS 客户端连到我们；本类负责：
- 维护连接、发送 action、按 echo 匹配响应
- 接收事件并投递到 asyncio 队列
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import uuid
from typing import Any

import httpx
from fastapi import WebSocket

from .config import settings
from .segments import describe

log = logging.getLogger("qqbot.onebot")


class OneBotError(RuntimeError):
    """网关调用失败（未连接 / 超时 / retcode 非 0）。"""


def event_to_record(ev: dict[str, Any], *, self_id: int | None = None) -> dict[str, Any] | None:
    post_type = ev.get("post_type")
    if post_type not in {"message", "message_sent"}:
        return None
    message = ev.get("message")
    sender = ev.get("sender") or {}
    user_id = ev.get("user_id")
    direction = "out" if post_type == "message_sent" else "in"
    if self_id and user_id and int(user_id) == int(self_id):
        direction = "out"
    return {
        "time_": int(ev.get("time") or time.time()),
        "message_id": ev.get("message_id"),
        "message_type": ev.get("message_type"),
        "sub_type": ev.get("sub_type"),
        "group_id": ev.get("group_id"),
        "user_id": user_id,
        "sender_name": sender.get("nickname"),
        "sender_card": sender.get("card"),
        "direction": direction,
        "raw_message": ev.get("raw_message") or describe(message),
        "segments": message if isinstance(message, list) else [],
    }


class OneBotClient:
    def __init__(self) -> None:
        self._ws: WebSocket | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._http: httpx.AsyncClient | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = itertools.count(1)
        self._self_id_from_conn: int | None = None

        self.self_id: int | None = settings.ob_self_id or None
        self.connected_at: float | None = None
        self.last_event_at: float | None = None
        self.event_count = 0
        self.last_error: str | None = None
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)

    # ---------- 连接管理 ----------

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def attach(self, ws: WebSocket, self_id: str | None = None) -> None:
        self._loop = asyncio.get_running_loop()
        self._ws = ws
        self.connected_at = time.time()
        if self_id and str(self_id).isdigit():
            self._self_id_from_conn = int(self_id)
            self.self_id = int(self_id)
        log.info("gateway connected (self_id=%s)", self.self_id)
        # 注意：不能在收包循环启动前 await API 调用，否则 echo 永远回不来。
        # 先注册连接，登录信息交给后台任务补。
        asyncio.create_task(self._bootstrap())

    async def _bootstrap(self) -> None:
        try:
            info = await self.call("get_login_info", timeout=5)
            if isinstance(info, dict) and info.get("user_id"):
                self.self_id = int(info["user_id"])
                log.info("login info: %s (%s)", info.get("nickname"), self.self_id)
        except OneBotError as exc:
            self.last_error = str(exc)
            log.warning("get_login_info failed: %s", exc)

    async def detach(self, ws: WebSocket) -> None:
        if self._ws is ws:
            self._ws = None
            log.warning("gateway disconnected")
        for echo, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(OneBotError("gateway disconnected"))
            self._pending.pop(echo, None)

    # ---------- 入站 ----------

    async def handle(self, payload: dict[str, Any]) -> None:
        if "echo" in payload:
            fut = self._pending.pop(str(payload["echo"]), None)
            if fut is not None and not fut.done():
                fut.set_result(payload)
            return

        self.event_count += 1
        self.last_event_at = time.time()
        post_type = payload.get("post_type")
        if post_type == "meta_event" and payload.get("meta_event_type") == "lifecycle":
            self_id = payload.get("self_id")
            if self_id:
                self.self_id = int(self_id)
        try:
            self.events.put_nowait(payload)
        except asyncio.QueueFull:
            log.error("event queue full, dropping %s event", post_type)

    # ---------- 出站 ----------

    async def call(self, action: str, *, timeout: float | None = None, **params: Any) -> Any:
        timeout = timeout or settings.call_timeout
        if self._ws is not None:
            payload = await self._call_ws(action, params, timeout)
        elif settings.ob_http_base:
            payload = await self._call_http(action, params, timeout)
        else:
            raise OneBotError("gateway 未连接：请先启动 NapCat 并配置反向 WS")
        return self._unwrap(action, payload)

    async def _call_ws(self, action: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        assert self._ws is not None and self._loop is not None
        echo = f"{next(self._seq)}-{uuid.uuid4().hex[:8]}"
        fut: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        self._pending[echo] = fut
        try:
            await self._ws.send_text(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise OneBotError(f"{action} 超时（{timeout}s 无响应）") from None
        finally:
            self._pending.pop(echo, None)

    async def _call_http(self, action: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if self._http is None:
            headers = {}
            if settings.ob_token:
                headers["Authorization"] = f"Bearer {settings.ob_token}"
            self._http = httpx.AsyncClient(base_url=settings.ob_http_base, headers=headers, timeout=timeout)
        try:
            resp = await self._http.post(f"/{action}", json=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as exc:
            raise OneBotError(f"{action} HTTP 调用失败: {exc}") from exc

    @staticmethod
    def _unwrap(action: str, payload: dict[str, Any]) -> Any:
        if payload.get("status") == "failed" or payload.get("retcode", 0) not in (0, 1):
            wording = payload.get("wording") or payload.get("message") or payload.get("retcode")
            raise OneBotError(f"{action} 失败: {wording}")
        return payload.get("data")

    # ---------- 查询封装 ----------

    async def get_login_info(self) -> dict[str, Any]:
        return await self.call("get_login_info")

    async def get_status(self) -> dict[str, Any]:
        return await self.call("get_status")

    async def get_friend_list(self, no_cache: bool = False) -> list[dict[str, Any]]:
        return await self.call("get_friend_list", no_cache=no_cache)

    async def get_group_list(self, no_cache: bool = False) -> list[dict[str, Any]]:
        return await self.call("get_group_list", no_cache=no_cache)

    async def get_group_member_list(self, group_id: int) -> list[dict[str, Any]]:
        return await self.call("get_group_member_list", group_id=group_id)

    async def get_stranger_info(self, user_id: int) -> dict[str, Any]:
        return await self.call("get_stranger_info", user_id=user_id)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "self_id": self.self_id,
            "connected_at": self.connected_at,
            "last_event_at": self.last_event_at,
            "event_count": self.event_count,
            "pending_calls": len(self._pending),
            "last_error": self.last_error,
        }
