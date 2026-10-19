"""语音识别：QQ 语音其实是 SILK（文件名常骗人写成 .amr）。

链路：
    silk 文件（可能开头多一个 0x02/0x03）
      → strip_lead() 剥掉多出来的字节
      → silk_v3_decoder.exe 解成 16k 单声道 PCM
      → 包成 WAV
      → sherpa-onnx + SenseVoice 转文字（全程本地，不花钱）

参考：kn007/silk-v3-decoder（预编译解码器）、k2-fsa/sherpa-onnx 的 SenseVoice 模型。
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import logging
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("qqbot.asr")

SILK_MAGIC = b"#!SILK_V3"


class AsrError(RuntimeError):
    pass


def is_silk(raw: bytes) -> bool:
    return raw.startswith(SILK_MAGIC) or raw[1:].startswith(SILK_MAGIC)


def strip_lead(raw: bytes) -> tuple[bytes, int]:
    """QQ 存下来的语音最前面会多一个字节（见过 0x02 / 0x03），不剥掉解码器会拒收。"""
    if raw.startswith(SILK_MAGIC):
        return raw, 0
    if raw[1:].startswith(SILK_MAGIC):
        return raw[1:], 1
    raise AsrError("不是 SILK：开头既不是 #!SILK_V3，也不是多一个字节的 #!SILK_V3")


class Asr:
    """SILK → WAV → 文字。首次调用时才加载模型。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model_dir = Path(settings.asr_model_dir)
        self.silk_exe = Path(settings.asr_silk_exe)
        self.voice_dir = Path(settings.voice_dir)
        self.voice_dir.mkdir(parents=True, exist_ok=True)
        self.stats: dict[str, Any] = {
            "decoded": 0,
            "transcribed": 0,
            "failed": 0,
            "cached": 0,
            "chunks": 0,
            "fallbacks": 0,
        }
        self._recognizer: Any = None
        self._fallback: Any = None
        self._load_error: str = ""
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ 状态
    @property
    def engine(self) -> str:
        return (self.settings.asr_engine or "sensevoice").lower()

    @property
    def active_dir(self) -> Path:
        if self.engine == "paraformer":
            return Path(self.settings.asr_paraformer_dir)
        return self.model_dir

    @property
    def enabled(self) -> bool:
        model = self.active_dir
        return bool(
            self.settings.asr_enabled
            and self.silk_exe.exists()
            and (model / "model.int8.onnx").exists()
            and (model / "tokens.txt").exists()
        )

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "loaded": self._recognizer is not None,
            "load_error": self._load_error,
            "engine": self.engine,
            "language": self.settings.asr_language,
            "model_dir": str(self.active_dir),
            "silk_exe": str(self.silk_exe),
            "threads": self.settings.asr_threads,
            **self.stats,
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.settings.asr_timeout, follow_redirects=True)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # ------------------------------------------------------------ 解码 + 识别
    def _load(self) -> Any:
        if self._recognizer is not None:
            return self._recognizer
        if not self.enabled:
            raise AsrError("语音识别未启用或模型未就绪")
        import sherpa_onnx  # 延迟导入，没装也不影响主服务

        model = self.active_dir
        try:
            if self.engine == "paraformer":
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_paraformer(
                    paraformer=str(model / "model.int8.onnx"),
                    tokens=str(model / "tokens.txt"),
                    num_threads=max(1, int(self.settings.asr_threads)),
                    sample_rate=16000,
                    feature_dim=80,
                    decoding_method="greedy_search",
                    debug=False,
                )
            else:
                self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                    model=str(model / "model.int8.onnx"),
                    tokens=str(model / "tokens.txt"),
                    num_threads=max(1, int(self.settings.asr_threads)),
                    use_itn=True,
                    debug=False,
                    language=self.settings.asr_language or "auto",
                )
        except Exception as exc:  # noqa: BLE001
            self._load_error = str(exc)
            raise AsrError(f"ASR 模型加载失败（{self.engine}）: {exc}") from exc
        return self._recognizer

    def _decode_silk(self, src: Path, dst_wav: Path) -> Path:
        raw = src.read_bytes()
        data, skipped = strip_lead(raw)
        if skipped:
            log.info("SILK 前缀多出 %s 字节，已剥掉: %s", skipped, src.name)
        tmp_silk = dst_wav.with_name(dst_wav.stem + ".tmp.silk")
        tmp_pcm = dst_wav.with_name(dst_wav.stem + ".tmp.pcm")
        tmp_silk.write_bytes(data)
        tmp_pcm.unlink(missing_ok=True)
        try:
            proc = subprocess.run(
                [str(self.silk_exe), str(tmp_silk), str(tmp_pcm), "-fs_API", "16000"],
                capture_output=True,
                timeout=60,
            )
            if proc.returncode != 0 or not tmp_pcm.exists():
                raise AsrError(f"SILK 解码失败 rc={proc.returncode}")
            with wave.open(str(dst_wav), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(tmp_pcm.read_bytes())
        finally:
            tmp_silk.unlink(missing_ok=True)
            tmp_pcm.unlink(missing_ok=True)
        self.stats["decoded"] += 1
        return dst_wav

    def _load_fallback(self) -> Any:
        """主引擎识别为空时用的备用引擎（默认 paraformer）。"""
        if self._fallback is not None:
            return self._fallback
        name = (self.settings.asr_fallback or "").lower()
        if not name or name == self.engine:
            return None
        model = Path(self.settings.asr_paraformer_dir) if name == "paraformer" else self.model_dir
        if not (model / "model.int8.onnx").exists() or not (model / "tokens.txt").exists():
            return None
        import sherpa_onnx

        if name == "paraformer":
            self._fallback = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=str(model / "model.int8.onnx"),
                tokens=str(model / "tokens.txt"),
                num_threads=max(1, int(self.settings.asr_threads)),
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                debug=False,
            )
        else:
            self._fallback = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=str(model / "model.int8.onnx"),
                tokens=str(model / "tokens.txt"),
                num_threads=max(1, int(self.settings.asr_threads)),
                use_itn=True,
                debug=False,
                language=self.settings.asr_language or "auto",
            )
        return self._fallback

    def _decode_chunk(self, recognizer: Any, rate: int, waveform: list[float]) -> Any:
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, waveform)
        recognizer.decode_stream(stream)
        return stream.result

    def _recognize(self, wav_path: Path) -> tuple[str, dict[str, Any]]:
        """识别；超过 asr_chunk_seconds 的长语音切块拼接，返回 (文字, 元信息)。"""
        recognizer = self._load()
        with wave.open(str(wav_path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise AsrError("WAV 不是 16-bit 单声道")
            rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        samples = array.array("h")
        samples.frombytes(frames)
        waveform = [s / 32768.0 for s in samples]

        chunk_seconds = max(1.0, float(self.settings.asr_chunk_seconds or 30))
        limit = int(rate * chunk_seconds)
        pieces: list[str] = []
        meta: dict[str, Any] = {}
        total = len(waveform)
        if total <= limit:
            result = self._decode_chunk(recognizer, rate, waveform)
            pieces.append(str(result.text or ""))
            meta = {"lang": getattr(result, "lang", ""), "emotion": getattr(result, "emotion", ""), "event": getattr(result, "event", "")}
            self.stats["transcribed"] += 1
        else:
            # 切块：每块之间留 0.1s 重叠，避免把字切掉
            step = max(1, limit - int(rate * 0.1))
            for start in range(0, total, step):
                end = min(total, start + limit)
                result = self._decode_chunk(recognizer, rate, waveform[start:end])
                pieces.append(str(result.text or ""))
                self.stats["chunks"] += 1
                self.stats["transcribed"] += 1
                if not meta:
                    meta = {
                        "lang": getattr(result, "lang", ""),
                        "emotion": getattr(result, "emotion", ""),
                        "event": getattr(result, "event", ""),
                    }
                if end >= total:
                    break

        text = _clean_text(_merge_pieces(pieces))
        if not text:
            backup = self._load_fallback()
            if backup is not None:
                try:
                    result = self._decode_chunk(backup, rate, waveform if total <= limit else waveform[:limit])
                    backup_text = _clean_text(str(result.text or ""))
                    if backup_text:
                        self.stats["fallbacks"] += 1
                        log.info("主引擎没听出内容，备用引擎（%s）识别为: %r", self.settings.asr_fallback, backup_text[:40])
                        text = backup_text
                except Exception:  # noqa: BLE001
                    log.exception("备用引擎识别失败")
        return text, meta

    def _cache_path(self, digest: str) -> Path:
        return self.voice_dir / f"{digest}.asr.json"

    def _transcribe_path(self, src: Path) -> tuple[str, dict[str, Any]]:
        digest = hashlib.md5(src.read_bytes()).hexdigest()
        cached = self._cache_path(digest)
        if self.settings.asr_cache and cached.exists():
            try:
                import json

                data = json.loads(cached.read_text(encoding="utf-8"))
                if data.get("engine") == self.engine and data.get("text"):
                    self.stats["cached"] += 1
                    return str(data["text"]), dict(data.get("meta") or {})
            except Exception:  # noqa: BLE001
                log.warning("语音缓存读取失败，忽略: %s", cached.name)
        wav_path = self.voice_dir / f"{src.stem}.16k.wav"
        self._decode_silk(src, wav_path)
        text, meta = self._recognize(wav_path)
        if text and self.settings.asr_cache:
            try:
                import json

                cached.write_text(
                    json.dumps({"text": text, "meta": meta, "engine": self.engine}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception:  # noqa: BLE001
                log.warning("语音缓存写入失败: %s", cached.name)
        return text, meta

    async def transcribe_file_meta(self, path: str | Path) -> tuple[str, dict[str, Any]]:
        if not self.enabled:
            return "", {}
        src = Path(path)
        if not src.exists():
            raise AsrError(f"语音文件不存在: {src}")
        started = time.time()
        try:
            text, meta = await asyncio.get_running_loop().run_in_executor(None, self._transcribe_path, src)
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            log.warning("语音识别失败 %s: %s", src.name, exc)
            return "", {}
        log.info("语音识别完成 %s (%.2fs) lang=%s emotion=%s: %r", src.name, time.time() - started, meta.get("lang", ""), meta.get("emotion", ""), text[:60])
        return text, meta

    async def transcribe_file(self, path: str | Path) -> str:
        text, _ = await self.transcribe_file_meta(path)
        return text

    async def transcribe_url(self, url: str) -> str:
        if not self.enabled or not url:
            return ""
        try:
            resp = await self._http().get(url)
            resp.raise_for_status()
            data = resp.content
        except Exception as exc:  # noqa: BLE001
            self.stats["failed"] += 1
            log.warning("语音下载失败: %s", exc)
            return ""
        if not data or len(data) > self.settings.asr_max_bytes:
            self.stats["failed"] += 1
            return ""
        digest = hashlib.md5(data).hexdigest()[:16]
        target = self.voice_dir / f"{digest}.silk"
        if not target.exists():
            target.write_bytes(data)
        return await self.transcribe_file(target)


def _merge_pieces(pieces: list[str]) -> str:
    """把切块识别结果拼起来；相邻块因重叠而重复的字只保留一份。"""
    out = ""
    for piece in pieces:
        piece = str(piece or "").strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        # 找 out 的结尾和 piece 的开头最长公共部分
        overlap = 0
        for size in range(min(len(out), len(piece)), 1, -1):
            if out.endswith(piece[:size]):
                overlap = size
                break
        out += piece[overlap:]
    return out


def _clean_text(text: str) -> str:
    """去掉多余空白、重复标点，SenseVoice 偶尔会在中文之间塞空格。"""
    raw = " ".join(str(text or "").split())
    # 中文字符之间的空格去掉，保留英文单词间隔
    import re

    raw = re.sub(r"(?<=[\u4e00-\u9fff，。！？、；：])\s+(?=[\u4e00-\u9fff，。！？、；：])", "", raw)
    raw = re.sub(r"([，。！？、])\1+", r"\1", raw)
    return raw.strip()

