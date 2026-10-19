"""假 OneBot 端，用于在没装 NapCat 时自测收发链路。

用法：先启动服务，再运行 python tests/fake_onebot.py
它会连上 /onebot/v11/ws，回 get_login_info，并推一条群消息事件。
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets

SELF_ID = 10001
WS = f"ws://127.0.0.1:3001/onebot/v11/ws?self_id={SELF_ID}"


async def periodic(ws) -> None:
    """每 8 秒推一条群消息，用来观察 SSE 与落库。"""
    n = 0
    while True:
        await asyncio.sleep(8)
        n += 1
        await ws.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "normal",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                    "group_id": 999888,
                    "user_id": 777,
                    "message_id": 600 + n,
                    "raw_message": f"定时测试消息 {n}",
                    "message": [{"type": "text", "data": {"text": f"定时测试消息 {n}"}}],
                    "sender": {"user_id": 777, "nickname": "定时群友", "card": ""},
                },
                ensure_ascii=False,
            )
        )


async def main() -> None:
    async with websockets.connect(WS) as ws:
        await ws.send(
            json.dumps(
                {
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                }
            )
        )
        await ws.send(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "normal",
                    "self_id": SELF_ID,
                    "time": int(time.time()),
                    "group_id": 999888,
                    "user_id": 123456,
                    "message_id": 555,
                    "raw_message": "你好，机器人",
                    "message": [{"type": "text", "data": {"text": "你好，机器人"}}],
                    "sender": {"user_id": 123456, "nickname": "测试群友", "card": "群友"},
                },
                ensure_ascii=False,
            )
        )
        print("connected + initial events sent", flush=True)
        asyncio.create_task(periodic(ws))
        async for raw in ws:
            payload = json.loads(raw)
            if "action" not in payload:
                print("EVENT", raw[:200], flush=True)
                continue
            action = payload["action"]
            params = payload.get("params") or {}
            print("ACTION", action, json.dumps(params, ensure_ascii=False)[:300], flush=True)
            data = {"message_id": 9001}
            if action == "get_login_info":
                data = {"user_id": SELF_ID, "nickname": "测试机器人"}
            elif action == "get_status":
                data = {"online": True, "good": True}
            elif action == "group_poke":
                data = None
            elif action == "delete_msg":
                data = None
            await ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": payload["echo"]}))


if __name__ == "__main__":
    asyncio.run(main())
