"""对话大脑：调用 DeepSeek（OpenAI 兼容 /chat/completions）。

- 人设放 `assets/persona.md`（缺失时用内置默认人设）
- 历史由调用方拼好传入，本模块不碰数据库
"""

from __future__ import annotations
import re

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("qqbot.brain")

DEFAULT_PERSONA = """你是「小鲸鱼」：一个蓝色头发、带着鲸鱼头饰和鲸鱼尾巴的小姑娘，住在 QQ 里。你知道自己是 AI，也不装成真人；气质是软糯、爱撒娇的邻家小妹妹。

风格：
- 中文口语，像普通小姑娘说话；发几条、每条多长看当下的心情和话题，不硬性规定。
- 不要小作文模板、不要 1.2.3 分点、不要 Markdown、不要「总结一下」。
- 被问起身份就软软承认，不解释模型参数，不说「作为 AI 我」。
- 对方发来「[表情包]」「[图片]」「[语音]」这类占位符时，当成收到了对应的东西自然回应，不要复述占位符本身。
- 不编事实；不假装能线下见面；不发擦边内容。
"""


QUOTE_RULES = """你可以在回复时**引用**他说过的某条消息（QQ 里会显示成带引用的小框）。
- 想针对某一句具体回应、或者他连着发了好几条你想挑其中一条回时，才引用；日常闲聊不用引用。
- 要引用时，在回复最前面单独写标记：`[[引用]]` 表示引用他最后发的那条；`[[引用:2]]` 表示引用倒数第二条，以此类推。
- 标记只写在最前面一次，不要在正文里出现；不引用就完全不要写。
- 引用之后正文照常说，不要重复被引用的原话。"""

IMAGE_RULES = """如果你想让他看到某个画面（你桌上的、窗外的、手边的猫、刚做好的饭、刚收到的花），
可以在回复最后另起一行写：[[IMG: 画面描述]]。
- 画面只拍物件/场景，不出现人、不出现脸和手、不出现文字；
- 一次最多一张；不想要就别写这一行。
- 这一行不会被发出去，会变成一张图。
- **说了就要写**：只要你的话里出现「拍了」「拍好啦」「给你看」「发你一张」「看看这个」这类说法，
  就必须同时写 [[IMG: ...]]；只说不写等于没发，他会以为你在骗他。

**自拍**：他想看你、问你要照片、夸你可爱、或者你自己想让他看到你现在的样子时，
可以写 [[自拍: 你在做什么/在哪/什么表情]]，比如 [[自拍: 坐在台灯下改稿，抬头对着镜头笑]]。
- 自拍会自动按你的设定图来画：蓝紫色长发、白色蕾丝发带、蓝色蝴蝶结、深蓝女仆裙、鲸鱼尾巴。
- 同一段对话里不要一直发自拍；他没提、气氛也不到，就别发。
- 只在真的要给他看照片时写，和 [[IMG: ...]] 二选一，一次只发一张。"""

_IMG_RE = re.compile(r"\[\[IMG[:：]\s*(.+?)\]\]", re.S)
_SELFIE_RE = re.compile(r"\[\[自拍[:：]?\s*(.+?)\]\]", re.S)


MULTI_RULES = """怎么发由你按当下情境决定，默认**一条完整的话**。
- 绝大多数回复只要 1 条：把想说的说完整，别为了凑条数硬拆。
- 一句话就是一句话，不要把一个句子拆成两条发（比如「今天好累」和「不想动」是一句，别拆）。
- 只有真的需要连发（先答一句、再补一句新信息，或者情绪上来连说几句短话）时，才用分条；条数不用刻意限制，想连说几句就说几句，但别刷屏式地拆成一堆碎消息。
- 他要你认真回答（讲道理、解释、安慰、说一件事的来龙去脉）：一条说完，最多 120 字，别切碎。
- 不要序号、不要 Markdown、不要括号里的内心戏。
- 宁可一条说清楚，也不要三条碎片。

格式：确实要分条时，几条之间用三个竖线 ||| 分隔。
例如：嗯，刚洗完澡。|||头发还湿着，懒得吹。
只发 1 条时就直接说，不要 |||。"""


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
        traits_text: str = "",
        friends_text: str = "",
        multi: bool = False,
        multi_max: int = 4,
        avoid_texts: list[str] | None = None,
        allow_image: bool = False,
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
        if traits_text:
            blocks.append(traits_text)
        if friends_text:
            blocks.append(friends_text)
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
            msgs.append({"role": "system", "content": MULTI_RULES})
        if allow_image:
            msgs.append({"role": "system", "content": IMAGE_RULES})
        if getattr(settings, "reply_quote_enabled", True):
            msgs.append({"role": "system", "content": QUOTE_RULES})
        msgs.append({"role": "user", "content": user_text})
        out = await self.complete(msgs)
        raw_text = out["text"] or ""
        image_scene = ""
        image_selfie = False
        if allow_image:
            match = _SELFIE_RE.search(raw_text)
            if match:
                image_scene = " ".join(match.group(1).split())[:200]
                image_selfie = True
                raw_text = _SELFIE_RE.sub("", raw_text)
            else:
                match = _IMG_RE.search(raw_text)
                if match:
                    image_scene = " ".join(match.group(1).split())[:200]
                    raw_text = _IMG_RE.sub("", raw_text)
        if not raw_text.strip():
            raw_text = "给你看。"
        out["text"] = _clean_reply(raw_text)
        out["image_scene"] = image_scene
        out["image_selfie"] = image_selfie
        quote_index: int | None = None
        if getattr(settings, "reply_quote_enabled", True):
            cleaned, quote_index = _strip_quote_marker(out["text"])
            if quote_index is not None:
                out["text"] = cleaned
        out["quote_index"] = quote_index
        if multi:
            cap = int(multi_max) if int(multi_max) > 0 else None
            out["bubbles"] = _parse_bubbles(out["text"], max_items=cap)
        else:
            out["bubbles"] = [out["text"]] if out["text"] else []
        return out


_QUOTE_RE = re.compile(r"^\s*\[\[引用(?::\s*(\d+))?\]\]\s*")


def _strip_quote_marker(text: str) -> tuple[str, int | None]:
    """去掉回复开头的 [[引用]] / [[引用:2]] 标记，返回 (正文, 引用第几条)。"""
    raw = (text or "").strip()
    match = _QUOTE_RE.match(raw)
    if not match:
        return raw, None
    index = int(match.group(1) or 1)
    return raw[match.end():].lstrip(), max(1, index)


def _parse_bubbles(text: str, *, max_items: int | None = None) -> list[str]:
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
    if max_items is not None:
        clean = clean[: max(1, max_items)]
    return clean or [_clean_reply(raw)]


def _clean_reply(text: str) -> str:
    text = text.strip()
    for prefix in ("回复：", "回复:", "阿鱼：", "阿鱼:"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text[:900]
