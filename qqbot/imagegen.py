"""给她的主动消息配一张「随手拍」。

当前实现用免 key 的 Pollinations（Flux）出图：慢一点、有队列，所以带重试，
失败了就让调用方退回纯文字。图片按 prompt 哈希缓存，裁掉底部水印条。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx

from .config import settings

log = logging.getLogger("qqbot.imagegen")

STYLE_PROMPTS = {
    "anime": "soft anime illustration, warm cinematic lighting, gentle colors, detailed cozy background",
    "watercolor": "watercolor illustration, soft edges, warm paper texture, gentle light",
    "photo": "realistic phone photo, natural light, shallow depth of field, candid, slightly grainy",
}

SELFIE_CHARACTER = (
    "a cute chibi anime girl, long deep blue-purple hair with a faint aqua gradient at the tips, straight bangs, "
    "white lace headdress with a small blue bow on each side, whale-fin shaped hair ornaments, "
    "big round blue-purple eyes, small stature, dark blue maid-style dress with white lace collar "
    "and a white lace apron embroidered with a tiny whale, white socks and shoes, "
    "a blue whale tail behind her"
)

NEGATIVE = (
    "no people, no faces, no hands, no text, no letters, no watermark, "
    "no logo, no signature, no border, no frame"
)


class ImageGen:
    """文生图。generate() 失败返回 None，调用方自行退回纯文字。"""

    def __init__(self) -> None:
        self.dir = Path(getattr(settings, "imagegen_dir", "data/outbox"))
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            log.exception("创建出图目录失败")
        self.stats: dict[str, Any] = {
            "requests": 0,
            "ok": 0,
            "failed": 0,
            "cropped": 0,
            "cached": 0,
            "last_ms": None,
            "last_error": None,
            "last_path": None,
            "last_prompt": None,
            "wan_ok": 0,
            "wan_failed": 0,
        }

    @property
    def provider(self) -> str:
        return str(getattr(settings, "imagegen_provider", "zhipu") or "zhipu").lower()

    @property
    def enabled(self) -> bool:
        if not getattr(settings, "imagegen_enabled", False):
            return False
        if self.provider in ("pollinations", "pollinations-flux"):
            return True
        if self.provider == "zhipu":
            return bool(str(getattr(settings, "imagegen_api_key", "") or "").strip())
        return False

    @property
    def selfie_provider(self) -> str:
        return str(getattr(settings, "imagegen_selfie_provider", "dashscope") or "dashscope").lower()

    @property
    def selfie_ready(self) -> bool:
        if self.selfie_provider != "dashscope":
            return self.enabled
        return bool(str(getattr(settings, "imagegen_wan_key", "") or "").strip())

    def wan_prompt_for(self, scene: str) -> str:
        scene = " ".join(str(scene or "").split())[:300]
        return (
            "只参考这张图里角色的长相、发型、发带、蝴蝶结、女仆裙和鲸鱼尾巴，不要照搬背景和姿势。"
            f"画面：她{scene}。半身自拍视角，竖构图，Q 版动漫插画风，暖色灯光，看镜头，"
            "背景自然有生活感，不要文字、不要水印。"
        )

    def prompt_for(self, scene: str, *, mood: str = "", selfie: bool = False) -> str:
        style = STYLE_PROMPTS.get(str(getattr(settings, "imagegen_style", "anime")), STYLE_PROMPTS["anime"])
        scene = " ".join(str(scene or "").split())[:300]
        if selfie:
            bits = [SELFIE_CHARACTER + ", selfie camera angle, looking at the camera", scene, style]
        else:
            bits = [scene, style]
        if mood:
            bits.insert(1, f"mood: {mood}")
        bits.append(NEGATIVE)
        return ", ".join(part for part in bits if part)

    async def generate(
        self, scene: str, *, mood: str = "", reuse: bool = True, selfie: bool = False
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        if selfie and not getattr(settings, "image_selfie_enabled", True):
            return None
        prompt = self.prompt_for(scene, mood=mood, selfie=selfie)
        use_wan = bool(selfie and self.selfie_ready)
        tag = "wan" if use_wan else self.provider
        digest = hashlib.md5((prompt + "|" + tag).encode("utf-8")).hexdigest()[:16]
        path = self.dir / f"gen_{digest}.jpg"
        self.stats["requests"] += 1
        if reuse and path.exists() and path.stat().st_size > 15000:
            self.stats["cached"] += 1
            return {"path": str(path), "prompt": prompt, "cached": True}
        raw = None
        if use_wan:
            raw = await self._fetch_wan(self.wan_prompt_for(scene))
        if not raw:
            raw = await self._fetch(prompt)
        if not raw:
            self.stats["failed"] += 1
            return None
        tmp = self.dir / f".gen_{digest}_raw.jpg"
        try:
            tmp.write_bytes(raw)
            if use_wan:
                self._save_plain(tmp, path)
            else:
                self._crop_watermark(tmp, path)
            self.stats["ok"] += 1
            self.stats["last_path"] = str(path)
            self.stats["last_prompt"] = prompt
            return {"path": str(path), "prompt": prompt, "bytes": path.stat().st_size}
        except Exception:  # noqa: BLE001
            log.exception("处理生成图失败")
            path.write_bytes(raw)
            self.stats["ok"] += 1
            self.stats["last_path"] = str(path)
            return {"path": str(path), "prompt": prompt, "bytes": len(raw)}
        finally:
            try:
                tmp.unlink()
            except Exception:  # noqa: BLE001
                pass

    def _save_plain(self, src: Path, dst: Path) -> None:
        """百炼返回的 PNG 本来就没有水印，转成 jpg 存下即可。"""
        from PIL import Image

        Image.open(src).convert("RGB").save(dst, quality=90, optimize=True)

    def _portrait_reference(self, ref: Path) -> bytes:
        """把横版设定图垫成 3:4 竖图再当参考，保证出图是竖构图。"""
        import io

        from PIL import Image

        image = Image.open(ref).convert("RGB")
        width, height = image.size
        target_h = int(width * 4 / 3)
        if height >= target_h:
            crop = image.crop((0, 0, width, target_h))
        else:
            corner = image.getpixel((1, 1))
            canvas = Image.new("RGB", (width, target_h), corner)
            canvas.paste(image, (0, (target_h - height) // 2))
            crop = canvas
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        return buffer.getvalue()

    async def _fetch_wan(self, prompt: str) -> bytes | None:
        """阿里云百炼 wan2.7-image：参考图 + 文字，保持角色一致。"""
        key = str(getattr(settings, "imagegen_wan_key", "") or "").strip()
        ref = Path(getattr(settings, "imagegen_reference", ""))
        if not key or not ref.exists():
            log.warning("百炼自拍跳过：缺少 key 或设定图 %s", ref)
            return None
        import base64
        import mimetypes

        data_uri = "data:image/png;base64," + base64.b64encode(self._portrait_reference(ref)).decode("utf-8")
        payload = {
            "model": str(getattr(settings, "imagegen_wan_model", "wan2.7-image")),
            "input": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"image": data_uri}, {"text": prompt}],
                    }
                ]
            },
            "parameters": {
                "size": str(getattr(settings, "imagegen_wan_size", "2K")),
                "n": 1,
                "watermark": False,
            },
        }
        base = str(getattr(settings, "imagegen_wan_base", "https://dashscope.aliyuncs.com/api/v1")).rstrip("/")
        url = f"{base}/services/aigc/multimodal-generation/generation"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        timeout = float(getattr(settings, "imagegen_timeout", 120))
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code != 200:
                        log.warning("百炼出图失败 %s: %s", resp.status_code, resp.text[:200])
                        if resp.status_code in (429, 500, 502, 503, 504):
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        return None
                    data = resp.json()
                    choices = (data.get("output") or {}).get("choices") or []
                    image_url = ""
                    for choice in choices:
                        for item in ((choice.get("message") or {}).get("content") or []):
                            if item.get("type") == "image" and item.get("image"):
                                image_url = item["image"]
                                break
                        if image_url:
                            break
                    if not image_url:
                        log.warning("百炼返回里没有图片：%s", str(data)[:200])
                        return None
                    got = await client.get(image_url)
                    if got.status_code == 200 and got.content:
                        self.stats["wan_ok"] = int(self.stats.get("wan_ok") or 0) + 1
                        return got.content
            except Exception as exc:  # noqa: BLE001
                log.warning("百炼出图异常（第 %s 次）：%r", attempt + 1, exc)
                await asyncio.sleep(2 * (attempt + 1))
        self.stats["wan_failed"] = int(self.stats.get("wan_failed") or 0) + 1
        return None

    def _crop_watermark(self, src: Path, dst: Path) -> None:
        from PIL import Image

        image = Image.open(src)
        width, height = image.size
        if height > 120:
            # 智谱 CogView 在右下角有「AI生成」水印，裁掉底部 8%；Pollinations 只裁 5%
            ratio = 0.92 if self.provider == "zhipu" else 0.955
            cut = int(height * ratio)
            top = int(height * 0.03) if self.provider == "zhipu" else 0
            image = image.crop((0, top, width, cut))
            self.stats["cropped"] += 1
        image.convert("RGB").save(dst, quality=86, optimize=True)

    async def _fetch(self, prompt: str) -> bytes | None:
        if self.provider == "zhipu":
            return await self._fetch_zhipu(prompt)
        return await self._fetch_pollinations(prompt)

    async def _fetch_zhipu(self, prompt: str) -> bytes | None:
        """智谱 CogView（OpenAI 风格 /images/generations，返回图片 URL）。"""
        key = str(getattr(settings, "imagegen_api_key", "") or "").strip()
        if not key:
            self.stats["last_error"] = "no IMAGEGEN_API_KEY"
            return None
        timeout = float(getattr(settings, "imagegen_timeout", 120) or 120)
        retries = max(1, int(getattr(settings, "imagegen_retries", 3) or 3))
        base = str(getattr(settings, "imagegen_base", "https://open.bigmodel.cn/api/paas/v4")).rstrip("/")
        url = base + "/images/generations"
        payload = {
            "model": str(getattr(settings, "imagegen_model", "cogview-3-flash") or "cogview-3-flash"),
            "prompt": prompt,
            "size": str(getattr(settings, "imagegen_size", "864x1152") or "864x1152"),
        }
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        last_error = None
        for attempt in range(retries):
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    data = response.json()
                    items = data.get("data") or []
                    image_url = str((items[0] or {}).get("url") or "") if items else ""
                    if image_url:
                        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                            image = await client.get(image_url)
                        if image.status_code == 200 and len(image.content) > 8000:
                            self.stats["last_ms"] = round((time.perf_counter() - started) * 1000)
                            self.stats["last_error"] = None
                            return image.content
                        last_error = f"download http {image.status_code} bytes={len(image.content)}"
                    else:
                        last_error = f"no url: {json.dumps(data, ensure_ascii=True)[:160]}"
                else:
                    last_error = f"http {response.status_code} {response.text[:160]}"
                    if response.status_code in (429, 500, 502, 503, 504):
                        await asyncio.sleep(3 * (attempt + 1))
                        continue
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
            log.warning("智谱出图失败(第 %s 次): %s", attempt + 1, last_error)
            await asyncio.sleep(2.0 * (attempt + 1))
        self.stats["last_error"] = last_error
        return None

    async def _fetch_pollinations(self, prompt: str) -> bytes | None:
        timeout = float(getattr(settings, "imagegen_timeout", 120) or 120)
        retries = max(1, int(getattr(settings, "imagegen_retries", 2) or 2))
        url = "https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt)
        params = {
            "width": 768,
            "height": 1024,
            "nologo": "true",
            "model": str(getattr(settings, "imagegen_model", "flux") or "flux"),
            "seed": int(time.time()) % 100000,
        }
        last_error = None
        for attempt in range(retries):
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    response = await client.get(url, params=params)
                if response.status_code == 200 and len(response.content) > 15000:
                    self.stats["last_ms"] = round((time.perf_counter() - started) * 1000)
                    self.stats["last_error"] = None
                    return response.content
                last_error = f"http {response.status_code} bytes={len(response.content)}"
                if response.status_code == 402:
                    # 免费额度：每 IP 45 秒一张，等够再重试
                    log.info("出图被限流，等待 50 秒后重试")
                    await asyncio.sleep(50)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
            log.warning("出图失败(第 %s 次): %s", attempt + 1, last_error)
            await asyncio.sleep(1.5 * (attempt + 1))
        self.stats["last_error"] = last_error
        return None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "selfie_provider": self.selfie_provider,
            "selfie_ready": self.selfie_ready,
            "selfie_model": str(getattr(settings, "imagegen_wan_model", "")),
            "style": str(getattr(settings, "imagegen_style", "anime")),
            "model": str(getattr(settings, "imagegen_model", "cogview-3-flash")),
            "dir": str(self.dir),
            "stats": dict(self.stats),
        }
