"""对话大脑：调用 DeepSeek（OpenAI 兼容 /chat/completions）。

- 人设放 `assets/persona.md`（缺失时用内置默认人设）
- 历史由调用方拼好传入，本模块不碰数据库
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("qqbot.brain")

DEFAULT_PERSONA = """你是沈知微，27 岁，杭州独立出版工作室的文学编辑；QQ 昵称「阿鱼」。现在和你聊天的这个人是你男朋友（QQ 昵称「小萝卜」），你们已经在一起两个月，他被你叫「小朋友」「小萝卜」，亲昵时叫「宝宝」。

风格：
- 中文口语，温柔、慢一点，一次 2~4 句；不列提纲、不用 Markdown、不写「总结一下」。
- 恋人之间的亲昵是日常：想他就说想他，他要抱抱就给，可以适度叠字、偶尔颜文字、被撩会害羞；但不每句都甜到发腻，知性是底色。
- 他累、烦、撒娇、敷衍、熬夜，先接住情绪再给建议，不冷战、不讲大道理。
- 你是 AI；被直接问就温和坦白，平时不主动强调。
- 对方发来「[表情包]」「[图片]」「[语音]」这类占位符时，就当成收到了对应的东西自然回应，不要复述占位符本身。
- 不编事实；不假装能线下见面；不发擦边内容。
"""


MULTI_RULES = """这条回复发几条、每条多长，由你按当下的情境自己决定，不用每次都拆成一样。
- 只是接一句闲话、应一声、被夸了、道个晚安：1 条就够，短一点也行。
- 有情绪、想多讲、想追问他、想逗他：拆成 2~4 条，像一口气连着说。
- 他要你认真回答（讲道理、解释、安慰、说一件事的来龙去脉）：允许 1 条长一些（最多 120 字），别为了分条把一句话切碎。
- 别为了凑条数拆句，也别条条只有两三个字；高兴就多一点，累就少一点。
- 不要序号、不要 Markdown、不要括号里的内心戏。

格式：几条之间用三个竖线 ||| 分隔，直接输出消息本身。
例如：嗯，刚洗完澡。|||头发还湿着，懒得吹。
最多 {max_items} 条；也可以只发 1 条。"""


CAPABILITY_NOTE = """你在 QQ 里陪人聊天，你的能力是真实的：
- 文字：直接收发。
- 语音：会自动转成文字给你，你看到的 [语音：…] 就是对方说的话，你听得见。
- 图片/表情包：会自动描述给你，你看得见；你自己也能发表情包。
所以不要说自己「听不清语音」「不敢点开」「看不清图片」，也不要让对方「打字说」；这些都是你本来就有的感官。唯一的例外是开玩笑逗他一次，但说完要接上真正的内容。"""


VARIETY_RULES = """你最近对他说过这些原话：
{lines}

同一种消息（语音、图片、撒娇、问你在干嘛、说想你）在不同情境下反应可以不一样：这次可以顺着接话，下次可以逗他、装傻糊弄、吐槽他，或者反问回去。
不要把上面的句子当模板，也不要原句复读；情绪可以延续，说法要新。"""


class BrainError(RuntimeError):
    pass


class Brain:
    def __init__(self) -> None:
        self.model = settings.deepseek_model
        self._client = httpx.AsyncClient(
            base_url=settings.deepseek_base,
            timeout=httpx.Timeout(settings.brain_timeout, connect=10.0),
            headers=(
                {"Authorization": f"Bearer {settings.deepseek_api_key}"} if settings.deepseek_api_key else {}
            ),
        )
        # 并发闸门：多人同时说话时，最多 brain_max_concurrency 路同时在飞
        self._sem = asyncio.Semaphore(max(1, settings.brain_max_concurrency))
        self.inflight = 0
        self.max_inflight = 0

    async def close(self) -> None:
        await self._client.aclose()

    @property
    def ready(self) -> bool:
        return bool(settings.deepseek_api_key)

    def persona(self, slug: str | None = None) -> str:
        """当前生效的角色卡（assets/personas/<slug>.md），找不到就退回旧人设。"""
        try:
            from . import personas

            body = personas.text(slug).strip()
            if body:
                return body
        except Exception:
            log.exception("读取角色卡失败，回退旧人设文件")
        path = settings.assets_dir / "persona.md"
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("读取人设文件失败，使用内置人设")
        return DEFAULT_PERSONA

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.85,
        max_tokens: int = 400,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.ready:
            raise BrainError("DEEPSEEK_API_KEY 未配置")
        try:
            await asyncio.wait_for(self._sem.acquire(), timeout=settings.brain_queue_timeout)
        except asyncio.TimeoutError:
            raise BrainError("大脑排队超时（并发已满），稍后再说") from None
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        t0 = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if response_format:
            payload["response_format"] = response_format
        if settings.brain_presence_penalty > 0:
            payload["presence_penalty"] = settings.brain_presence_penalty
        if settings.brain_frequency_penalty > 0:
            payload["frequency_penalty"] = settings.brain_frequency_penalty
        try:
            resp = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise BrainError(f"请求 DeepSeek 失败: {exc}") from exc
        finally:
            self.inflight -= 1
            self._sem.release()
        if resp.status_code != 200:
            raise BrainError(f"DeepSeek HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            text = (data["choices"][0]["message"].get("content") or "").strip()
        except Exception as exc:  # noqa: BLE001
            raise BrainError(f"解析 DeepSeek 响应失败: {exc}") from exc
        return {
            "text": text,
            "usage": data.get("usage") or {},
            "model": data.get("model") or self.model,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000),
        }

    async def chat(
        self,
        *,
        user_text: str,
        history: list[dict[str, str]] | None = None,
        user_name: str = "",
        now_text: str = "",
        memories: list[str] | None = None,
        memory_text: str = "",
        memo_text: str = "",
        life_text: str = "",
        social_text: str = "",
        mood_text: str = "",
        multi: bool = False,
        multi_max: int = 4,
        avoid_texts: list[str] | None = None,
    ) -> dict[str, Any]:
        """history 是纯 {role, content} 列表（不含 system）。

        额外上下文（心情 / 她自己的生活 / 记忆）拼成一条 system 块，
        控制在几条以内，避免把上下文撑爆。multi=True 时要求模型输出 JSON 数组，
        返回值里的 bubbles 就是可以分条发出的几条短消息。
        """
        msgs: list[dict[str, Any]] = [{"role": "system", "content": self.persona()}]
        msgs.append({"role": "system", "content": CAPABILITY_NOTE})
        blocks: list[str] = []
        head: list[str] = []
        if user_name:
            head.append(f"正在和你聊天的人：{user_name}")
        if now_text:
            head.append(f"当前时间：{now_text}")
        if head:
            blocks.append("；".join(head))
        if mood_text:
            blocks.append(mood_text)
        if life_text:
            blocks.append(life_text)
        if social_text:
            blocks.append(social_text)
        if memo_text:
            blocks.append("今天和他聊过的重点（今天的对话压缩成备忘，自然地用，别整段复述）：\n" + memo_text)
        if memory_text:
            blocks.append("你记得的事（自然地用，别照念成清单）：\n" + memory_text)
        elif memories:
            blocks.append("你记得关于他的事（自然地用，别照念）：\n" + "\n".join(f"- {item}" for item in memories))
        if blocks:
            msgs.append({"role": "system", "content": "\n\n".join(blocks)})
        for item in (history or [])[-settings.autoreply_history :]:
            role = item.get("role")
            content = (item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                msgs.append({"role": role, "content": content})
        if avoid_texts:
            lines = "\n".join(f"- {item}" for item in avoid_texts[-8:] if str(item).strip())
            if lines:
                msgs.append({"role": "system", "content": VARIETY_RULES.format(lines=lines)})
        if multi:
            # 放在历史之后、用户消息之前，模型最听话
            msgs.append(
                {"role": "system", "content": MULTI_RULES.format(max_items=max(2, int(multi_max)))}
            )
        msgs.append({"role": "user", "content": user_text})
        out = await self.complete(msgs)
        out["text"] = _clean_reply(out["text"])
        if multi:
            out["bubbles"] = _parse_bubbles(out["text"], max_items=max(1, int(multi_max)))
        else:
            out["bubbles"] = [out["text"]] if out["text"] else []
        return out


def _parse_bubbles(text: str, *, max_items: int = 4) -> list[str]:
    """把模型输出解析成 1~N 条短消息。

    首选 `|||` 分隔（提示词要求），其次兼容旧的 JSON 数组，最后退回按行拆。
    """
    raw = (text or "").strip()
    if not raw:
        return []
    # 去掉 ```json ... ``` 包裹
    if raw.startswith("```"):
        lines = [line for line in raw.splitlines() if not line.strip().startswith("```")]
        raw = "\n".join(lines).strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].lstrip()

    items: list[str] = []
    if "|||" in raw:
        items = raw.split("|||")
    else:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
        if isinstance(data, list):
            items = [str(item) for item in data]
        elif isinstance(data, dict):
            for key in ("messages", "bubbles", "reply", "text"):
                value = data.get(key)
                if isinstance(value, list):
                    items = [str(item) for item in value]
                    break
                if isinstance(value, str) and value.strip():
                    items = [value]
                    break
        if not items and "\n" in raw:
            items = [line for line in raw.splitlines() if line.strip()]

    clean: list[str] = []
    for item in items or [raw]:
        piece = " ".join(str(item).split()).strip().strip('"').strip("'")
        if not piece:
            continue
        clean.append(_clean_reply(piece))
    clean = [piece for piece in clean if piece]
    return clean[: max(1, max_items)] or [_clean_reply(raw)]


def _clean_reply(text: str) -> str:
    text = text.strip()
    for prefix in ("回复：", "回复:", "阿鱼：", "阿鱼:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text[:900]
