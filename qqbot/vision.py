"""识图：把 QQ 里的图片 / 表情包下载归档，交给多模态模型用一句话描述。

- 下载：OneBot 的 image/mface 段里带 url（rkey 会过期），必须收到就落盘。
- 缓存：按内容 md5 去重，同一张图只描述一次。
- 描述：走 DeepSeek 的 image_url 输入（deepseek-chat / deepseek-flash 都支持 image 输入）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Any

import httpx

from .config import Settings
from .store import Store

log = logging.getLogger("qqbot.vision")

PROMPT_VERSION = 3

DESCRIBE_PROMPT = (
    "这是 QQ 聊天里发来的一张图片。严格按两行输出，不要多余的话：\n"
    "【文字】把图中所有可见文字逐字抄下来（标题、数字、单位、人名、按钮文字都要，"
    "数字保留小数和单位；确实没有文字就写「无」）。\n"
    "【画面】用一句中文（30 字以内）说画面主体、表情或情绪。"
)

SCENE_ONLY_PROMPT = (
    "这是 QQ 聊天里发来的一张图片。图里的文字已经由 OCR 单独提取，你不用重复。"
    "只用一句中文（30 字以内）说画面主体、表情或情绪，不要客套。"
)

STICKER_PROMPT = (
    "这是 QQ 聊天里发来的一个表情包。严格按两行输出，不要多余的话：\n"
    "【文字】图上文字逐字抄下来，没有就写「无」。\n"
    "【表情】用一句中文（15 字以内）说表情和情绪。"
)


class VisionError(RuntimeError):
    pass


class Vision:
    """图片归档 + 描述。失败一律返回 None，让上层退回占位文本。"""

    def __init__(self, store: Store, settings: Settings, ocr=None) -> None:
        self.store = store
        self.settings = settings
        self.ocr = ocr  # async callable(Path) -> dict：NapCat 的 ocr_image
        self.inbox: Path = Path(settings.inbox_dir)
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.stats: dict[str, Any] = {"described": 0, "cached": 0, "failed": 0, "downloaded": 0}
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.vision_enabled and self.settings.deepseek_api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.settings.vision_timeout, follow_redirects=True
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------ 下载归档
    async def download(self, url: str) -> tuple[Path, str] | None:
        try:
            resp = await self._http().get(url)
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            log.warning("图片下载失败: %s", exc)
            return None
        if not data or len(data) > self.settings.vision_max_bytes:
            self.stats["failed"] += 1
            log.warning("图片过大或为空: %s bytes", len(data))
            return None
        digest = hashlib.md5(data).hexdigest()
        mime = (resp.headers.get("content-type") or "").split(";")[0].strip()
        suffix = mimetypes.guess_extension(mime) or ".jpg"
        if suffix == ".jpe":
            suffix = ".jpg"
        path = self.inbox / f"{digest[:16]}{suffix}"
        if not path.exists():
            path.write_bytes(data)
            self.stats["downloaded"] += 1
        return path, digest

    # ------------------------------------------------------------ 描述
    async def describe_bytes(
        self,
        data: bytes,
        *,
        mime: str = "image/jpeg",
        sticker: bool = False,
        ocr_text: str = "",
    ) -> str:
        if not self.enabled:
            raise VisionError("识图未启用")
        b64 = base64.b64encode(data).decode()
        prompt = STICKER_PROMPT if sticker else (SCENE_ONLY_PROMPT if ocr_text else DESCRIBE_PROMPT)
        payload = {
            "model": self.settings.vision_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 400,
        }
        headers = {
            "Authorization": f"Bearer {self.settings.deepseek_api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._http().post(
                f"{self.settings.deepseek_base}/chat/completions", json=payload, headers=headers
            )
            resp.raise_for_status()
            text = ((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            log.warning("识图调用失败: %s", exc)
            return ""
        text = str(text).strip()
        if ocr_text:
            # OCR 的文字最准，模型只负责画面；文字段不再依赖模型转录
            text = f"【文字】{ocr_text}\n{text}"
        return "\n".join(line.strip() for line in text.splitlines() if line.strip())

    # ------------------------------------------------------------ OCR
    async def ocr_of(self, path: Path) -> str:
        """调 NapCat 内置 OCR（Windows 截图 OCR 服务），失败返回空串。"""
        if not (self.settings.vision_ocr_enabled and self.ocr is not None):
            return ""
        try:
            result = await self.ocr(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR 调用失败: %s", exc)
            return ""
        self.stats["ocr"] = self.stats.get("ocr", 0) + 1
        return _ocr_text(result)

    async def describe_url(
        self,
        url: str,
        *,
        kind: str = "image",
        sender_id: int | None = None,
        message_id: int | None = None,
    ) -> str | None:
        """下载 + 描述，返回结构化文字；失败返回 None。"""
        if not self.enabled:
            return None
        sticker = kind in ("mface", "sticker")
        got = await self.download(url)
        if got is None:
            return None
        path, digest = got
        cached = self.store.image_by_digest(digest)
        if cached and cached.get("summary") and int(cached.get("summary_version") or 0) >= PROMPT_VERSION:
            self.stats["cached"] += 1
            return str(cached["summary"])
        ocr_text = "" if sticker else await self.ocr_of(path)
        data = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
        summary = await self.describe_bytes(data, mime=mime, sticker=sticker, ocr_text=ocr_text)
        if not summary:
            return None
        self.store.add_image(
            digest=digest,
            path=str(path),
            kind="sticker" if sticker else "image",
            summary=summary,
            sender_id=sender_id,
            message_id=message_id,
            version=PROMPT_VERSION,
        )
        self.stats["described"] += 1
        log.info("识图完成 mid=%s kind=%s summary=%r", message_id, kind, summary[:80])
        return summary

    async def describe_file(self, path: str | Path, *, sticker: bool = False) -> str:
        p = Path(path)
        if not p.exists():
            raise VisionError(f"文件不存在: {p}")
        ocr_text = "" if sticker else await self.ocr_of(p)
        mime = mimetypes.guess_type(str(p))[0] or "image/jpeg"
        return await self.describe_bytes(
            p.read_bytes(), mime=mime, sticker=sticker, ocr_text=ocr_text
        )


def _ocr_text(result: Any) -> str:
    """从 NapCat ocr_image 的返回里拼出纯文本（按 y/x 排序还原阅读顺序）。"""
    if isinstance(result, str):
        return " ".join(result.split())
    if isinstance(result, list):
        blocks = [item for item in result if isinstance(item, dict)]
        if blocks:
            def _pos(item: dict[str, Any]) -> tuple[float, float]:
                pt = item.get("pt1") or item.get("pt2") or {}
                try:
                    return float(pt.get("y") or 0), float(pt.get("x") or 0)
                except (TypeError, ValueError):
                    return 0.0, 0.0

            blocks.sort(key=_pos)
            parts = [str(item.get("text") or "").strip() for item in blocks]
            return " ".join(" ".join(p for p in parts if p).split())
        parts = [str(v).strip() for v in result if str(v).strip()]
        return " ".join(" ".join(parts).split())
    if not isinstance(result, dict):
        return ""
    for key in ("texts", "text", "result", "words"):
        value = result.get(key)
        if isinstance(value, (str, list)):
            text = _ocr_text(value)
            if text:
                return text
    return ""
