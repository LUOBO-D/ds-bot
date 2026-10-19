"""鉴权与限速。"""

from __future__ import annotations

import secrets
import time
from collections import deque

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer(auto_error=False, description="ADMIN_TOKEN")


async def require_token(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    token: str | None = Query(default=None, include_in_schema=False),
) -> str:
    supplied = ""
    if creds is not None and creds.credentials:
        supplied = creds.credentials.strip()
    if not supplied and token:
        supplied = token.strip()
    if not supplied or not secrets.compare_digest(supplied, settings.admin_token):
        raise HTTPException(
            status_code=401,
            detail="无效 token：请在 .env 里查看 ADMIN_TOKEN，用 Authorization: Bearer <token> 调用",
        )
    return "ok"


class RateLimiter:
    """两个维度：单个会话最小间隔 + 全局每分钟上限。"""

    def __init__(self, min_interval: float, per_minute: int) -> None:
        self.min_interval = min_interval
        self.per_minute = per_minute
        self._last: dict[str, float] = {}
        self._window: deque[float] = deque()

    def check(self, key: str) -> None:
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last < self.min_interval:
            wait = self.min_interval - (now - last)
            raise HTTPException(status_code=429, detail=f"发得太快，{wait:.1f}s 后重试")
        while self._window and now - self._window[0] > 60:
            self._window.popleft()
        if len(self._window) >= self.per_minute:
            raise HTTPException(status_code=429, detail=f"触发全局限速（{self.per_minute}/分钟）")
        self._last[key] = now
        self._window.append(now)


limiter = RateLimiter(settings.min_send_interval, settings.max_sends_per_minute)
