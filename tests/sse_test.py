"""验证 /api/events 能实时推送网关事件（依赖假网关每 8s 一条的定时消息）。"""

from __future__ import annotations

import json
import sys
import threading
import time

import httpx

BASE = "http://127.0.0.1:3001"


def _token() -> str:
    with open(".env", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise SystemExit("ADMIN_TOKEN 不在 .env 里")


def main() -> int:
    token = _token()
    got: dict = {}

    def reader() -> None:
        with httpx.stream(
            "GET",
            f"{BASE}/api/events",
            params={"token": token},
            timeout=httpx.Timeout(30.0, read=30.0),
        ) as resp:
            resp.raise_for_status()
            current = ""
            for line in resp.iter_lines():
                if line.startswith("event:"):
                    current = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    payload = json.loads(line.split(":", 1)[1].strip())
                    if not got:
                        got["first"] = {"event": current, "data": payload}
                    if current == "message":
                        got["message"] = payload
                        return

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    deadline = time.time() + 20
    while time.time() < deadline and "message" not in got:
        time.sleep(0.2)

    if "first" not in got:
        print("[FAIL] sse 20s 内没有收到任何事件")
        return 1
    print(f"[OK]   sse_hello event={got['first']['event']}")
    if "message" not in got:
        print("[FAIL] sse 没等到 message 事件（20s）")
        return 1
    msg = got["message"]
    print(
        "[OK]   sse_message group={group} user={user} text={text}".format(
            group=msg.get("group_id"),
            user=msg.get("user_id"),
            text=msg.get("raw_message"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
