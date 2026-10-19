"""端到端自测：对着运行中的服务 + 假 OneBot 端跑一遍主要接口。

用法：python tests/e2e.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:3001"

env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
TOKEN = env["ADMIN_TOKEN"]
H = {"Authorization": f"Bearer {TOKEN}", "X-Actor": "e2e"}

ok = 0
fail = 0


def check(name: str, resp: httpx.Response, expect=200):
    global ok, fail
    body = resp.text[:400]
    if resp.status_code == expect:
        ok += 1
        print(f"[OK]   {name} {resp.status_code} {body[:160]}")
    else:
        fail += 1
        print(f"[FAIL] {name} {resp.status_code} {body}")
    return resp


def send_pause(seconds: float = 1.1) -> None:
    """限速器对同一会话要求最小间隔，测试里跟着节流。"""
    time.sleep(seconds)


with httpx.Client(base_url=BASE, headers=H, timeout=30) as c:
    check("health", c.get("/api/health"))
    check("state", c.get("/api/state"))
    check("account", c.get("/api/account"))
    check("stickers", c.get("/api/stickers?tag=笑"))
    check("send_text", c.post("/api/send/text", json={"target": {"type": "group", "id": 999888}, "text": "自测：你好"}))
    send_pause()
    check("send_segments", c.post("/api/send", json={
        "target": {"type": "private", "id": 123456},
        "segments": [{"type": "text", "data": {"text": "带 @"}}, {"type": "at", "data": {"qq": "123456"}}, " 和表情 "],
    }))
    send_pause()
    check("send_sticker", c.post("/api/send/sticker", json={"target": {"type": "group", "id": 999888}, "tag": "笑"}))
    send_pause()
    check("send_sticker_face", c.post("/api/send/sticker", json={"target": {"type": "group", "id": 999888}, "face_id": 14}))
    send_pause()
    check("poke", c.post("/api/poke", json={"target": {"type": "group", "id": 999888}}))
    send_pause()
    check("emoji_like", c.post("/api/emoji/like", json={"message_id": 9001, "emoji_id": "76"}))
    check("messages", c.get("/api/messages?group_id=999888&limit=10"))
    check("audit", c.get("/api/audit?limit=5"))
    check("qzone_status", c.get("/api/qzone/status"), expect=200)
    check("auth_required", c.get("/api/state", headers={"Authorization": "Bearer wrong"}), expect=401)

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
