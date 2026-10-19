"""运行时配置。

所有配置走环境变量 / .env；ADMIN_TOKEN 缺失时自动生成并写回 .env。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"

load_dotenv(ENV_FILE)


def _ensure_admin_token() -> str:
    token = (os.getenv("ADMIN_TOKEN") or "").strip()
    if token:
        return token
    token = secrets.token_urlsafe(24)
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), "ADMIN_TOKEN", token, quote_mode="never")
    os.environ["ADMIN_TOKEN"] = token
    return token


def _csv(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    # 服务
    host: str = field(default_factory=lambda: os.getenv("QB_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("QB_PORT", "3001")))
    admin_token: str = field(default_factory=_ensure_admin_token)
    db_path: Path = field(default_factory=lambda: Path(os.getenv("QB_DB") or ROOT / "data" / "qqbot.db"))

    # OneBot 网关（NapCat）
    ws_path: str = field(default_factory=lambda: os.getenv("OB_WS_PATH", "/onebot/v11/ws"))
    ob_http_base: str | None = field(
        default_factory=lambda: (os.getenv("OB_HTTP_BASE") or "").strip().rstrip("/") or None
    )
    ob_token: str | None = field(default_factory=lambda: (os.getenv("OB_ACCESS_TOKEN") or "").strip() or None)
    ob_self_id: int = field(default_factory=lambda: int(os.getenv("QB_SELF_ID") or 0))
    call_timeout: float = field(default_factory=lambda: float(os.getenv("QB_CALL_TIMEOUT", "15")))

    # QQ 空间 sidecar
    qzone_base: str = field(
        default_factory=lambda: (os.getenv("QZONE_BASE") or "http://127.0.0.1:5700").rstrip("/")
    )
    qzone_token: str | None = field(
        default_factory=lambda: (os.getenv("QZONE_ACCESS_TOKEN") or "").strip() or None
    )

    # 资源
    sticker_dir: Path = field(default_factory=lambda: ROOT / "assets" / "stickers")
    # 表情包库：收到好友表情包自己判断收不收，聊天时偶尔发出去
    sticker_enabled: bool = field(default_factory=lambda: os.getenv("STICKER_ENABLED", "1") not in ("0", "false", "False"))
    sticker_max: int = field(default_factory=lambda: int(os.getenv("STICKER_MAX", "120")))
    sticker_judge_temperature: float = field(default_factory=lambda: float(os.getenv("STICKER_JUDGE_TEMPERATURE", "0.6")))
    sticker_send_probability: float = field(default_factory=lambda: float(os.getenv("STICKER_SEND_PROBABILITY", "0.3")))
    sticker_send_daily_max: int = field(default_factory=lambda: int(os.getenv("STICKER_SEND_DAILY_MAX", "12")))
    sticker_send_cooldown: int = field(default_factory=lambda: int(os.getenv("STICKER_SEND_COOLDOWN", "900")))
    assets_dir: Path = field(default_factory=lambda: ROOT / "assets")
    inbox_dir: Path = field(default_factory=lambda: ROOT / "data" / "inbox")

    # 多角色卡
    personas_dir: Path = field(default_factory=lambda: ROOT / "assets" / "personas")
    persona_default: str = field(default_factory=lambda: (os.getenv("PERSONA_DEFAULT") or "").strip())

    # 对话大脑（DeepSeek，OpenAI 兼容接口）
    deepseek_api_key: str = field(default_factory=lambda: (os.getenv("DEEPSEEK_API_KEY") or "").strip())
    deepseek_base: str = field(
        default_factory=lambda: (os.getenv("DEEPSEEK_BASE") or "https://api.deepseek.com").rstrip("/")
    )
    deepseek_model: str = field(default_factory=lambda: (os.getenv("DEEPSEEK_MODEL") or "deepseek-flash").strip())
    brain_timeout: float = field(default_factory=lambda: float(os.getenv("QB_BRAIN_TIMEOUT", "60")))
    brain_max_concurrency: int = field(default_factory=lambda: int(os.getenv("BRAIN_MAX_CONCURRENCY", "4")))
    brain_queue_timeout: float = field(default_factory=lambda: float(os.getenv("BRAIN_QUEUE_TIMEOUT", "30")))
    brain_presence_penalty: float = field(default_factory=lambda: float(os.getenv("BRAIN_PRESENCE_PENALTY", "0.4")))
    brain_frequency_penalty: float = field(default_factory=lambda: float(os.getenv("BRAIN_FREQUENCY_PENALTY", "0.3")))

    # 识图（图片 / 表情包）
    vision_enabled: bool = field(default_factory=lambda: os.getenv("VISION_ENABLED", "1") == "1")
    vision_model: str = field(default_factory=lambda: (os.getenv("VISION_MODEL") or "deepseek-chat").strip())
    vision_timeout: float = field(default_factory=lambda: float(os.getenv("VISION_TIMEOUT", "30")))
    vision_max_bytes: int = field(default_factory=lambda: int(os.getenv("VISION_MAX_BYTES", str(8 * 1024 * 1024))))
    vision_ocr_enabled: bool = field(default_factory=lambda: os.getenv("VISION_OCR_ENABLED", "1") == "1")

    # 语音识别（SILK → SenseVoice）
    asr_enabled: bool = field(default_factory=lambda: os.getenv("ASR_ENABLED", "1") == "1")
    asr_engine: str = field(default_factory=lambda: (os.getenv("ASR_ENGINE") or "sensevoice").strip().lower())
    asr_language: str = field(default_factory=lambda: (os.getenv("ASR_LANGUAGE") or "zh").strip())
    asr_model_dir: str = field(default_factory=lambda: (os.getenv("ASR_MODEL_DIR") or str(ROOT / "models" / "sense-voice")).strip())
    asr_paraformer_dir: str = field(default_factory=lambda: (os.getenv("ASR_PARAFORMER_DIR") or str(ROOT / "models" / "paraformer-zh")).strip())
    asr_silk_exe: str = field(default_factory=lambda: (os.getenv("ASR_SILK_EXE") or str(ROOT / "tools" / "silk" / "silk_v3_decoder.exe")).strip())
    asr_threads: int = field(default_factory=lambda: int(os.getenv("ASR_THREADS", "2")))
    asr_timeout: float = field(default_factory=lambda: float(os.getenv("ASR_TIMEOUT", "30")))
    asr_max_bytes: int = field(default_factory=lambda: int(os.getenv("ASR_MAX_BYTES", str(10 * 1024 * 1024))))
    asr_chunk_seconds: float = field(default_factory=lambda: float(os.getenv("ASR_CHUNK_SECONDS", "30")))
    asr_fallback: str = field(default_factory=lambda: (os.getenv("ASR_FALLBACK") or "paraformer").strip().lower())
    asr_cache: bool = field(default_factory=lambda: os.getenv("ASR_CACHE", "1") == "1")
    asr_emotion_hint: bool = field(default_factory=lambda: os.getenv("ASR_EMOTION_HINT", "1") == "1")
    voice_dir: str = field(default_factory=lambda: (os.getenv("VOICE_DIR") or str(ROOT / "data" / "voice")).strip())

    # 社交线（刷好友空间）
    social_enabled: bool = field(default_factory=lambda: os.getenv("SOCIAL_ENABLED", "1") == "1")
    social_browse_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("SOCIAL_BROWSE_INTERVAL_SECONDS", "5400"))
    )
    social_browse_daily_max: int = field(default_factory=lambda: int(os.getenv("SOCIAL_BROWSE_DAILY_MAX", "6")))
    social_browse_probability: float = field(
        default_factory=lambda: float(os.getenv("SOCIAL_BROWSE_PROBABILITY", "0.35"))
    )
    social_window_start: int = field(default_factory=lambda: int(os.getenv("SOCIAL_WINDOW_START", "9")))
    social_window_end: int = field(default_factory=lambda: int(os.getenv("SOCIAL_WINDOW_END", "23")))
    social_num: int = field(default_factory=lambda: int(os.getenv("SOCIAL_NUM", "8")))
    owner_uid: int = field(default_factory=lambda: int(os.getenv("OWNER_UID", "0")))
    social_judge_enabled: bool = field(default_factory=lambda: os.getenv("SOCIAL_JUDGE_ENABLED", "1") == "1")
    social_judge_min_interest: float = field(
        default_factory=lambda: float(os.getenv("SOCIAL_JUDGE_MIN_INTEREST", "0.5"))
    )
    social_auto_like_probability: float = field(
        default_factory=lambda: float(os.getenv("SOCIAL_AUTO_LIKE_PROBABILITY", "0.4"))
    )
    social_auto_comment_probability: float = field(
        default_factory=lambda: float(os.getenv("SOCIAL_AUTO_COMMENT_PROBABILITY", "0.12"))
    )
    social_chat_cooldown_seconds: int = field(
        default_factory=lambda: int(os.getenv("SOCIAL_CHAT_COOLDOWN_SECONDS", "120"))
    )

    # 实时自动回复
    autoreply_enabled: bool = field(default_factory=lambda: os.getenv("AUTO_REPLY_ENABLED", "1") == "1")
    autoreply_allowlist: tuple[str, ...] = field(default_factory=lambda: _csv("AUTO_REPLY_ALLOWLIST", ""))
    autoreply_gap: float = field(default_factory=lambda: float(os.getenv("AUTO_REPLY_GAP", "1.2")))
    autoreply_history: int = field(default_factory=lambda: int(os.getenv("AUTO_REPLY_HISTORY", "30")))
    autoreply_stale: float = field(default_factory=lambda: float(os.getenv("AUTO_REPLY_STALE", "90")))

    # 回复形态：允许她自己分条发、说完再补一句
    reply_multi: bool = field(default_factory=lambda: os.getenv("REPLY_MULTI", "1") == "1")
    reply_multi_max: int = field(default_factory=lambda: int(os.getenv("REPLY_MULTI_MAX", "4")))
    reply_bubble_delay_min: float = field(
        default_factory=lambda: float(os.getenv("REPLY_BUBBLE_DELAY_MIN", "0.5"))
    )
    reply_bubble_delay_max: float = field(
        default_factory=lambda: float(os.getenv("REPLY_BUBBLE_DELAY_MAX", "1.6"))
    )
    followup_enabled: bool = field(default_factory=lambda: os.getenv("FOLLOWUP_ENABLED", "1") == "1")
    followup_probability: float = field(default_factory=lambda: float(os.getenv("FOLLOWUP_PROBABILITY", "0.3")))
    followup_delay_min: float = field(default_factory=lambda: float(os.getenv("FOLLOWUP_DELAY_MIN", "25")))
    followup_delay_max: float = field(default_factory=lambda: float(os.getenv("FOLLOWUP_DELAY_MAX", "90")))

    # 长期记忆 / 情绪 / 生活线（自主意识）
    memory_recall_k: int = field(default_factory=lambda: int(os.getenv("MEMORY_RECALL_K", "8")))
    memory_memo_enabled: bool = field(
        default_factory=lambda: os.getenv("MEMORY_MEMO_ENABLED", "1") == "1"
    )
    memory_memo_min_msgs: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_MEMO_MIN_MSGS", "6"))
    )
    memory_memo_min_seconds: float = field(
        default_factory=lambda: float(os.getenv("MEMORY_MEMO_MIN_SECONDS", "180"))
    )
    memory_memo_max_msgs: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_MEMO_MAX_MSGS", "120"))
    )
    memory_memo_max_chars: int = field(
        default_factory=lambda: int(os.getenv("MEMORY_MEMO_MAX_CHARS", "900"))
    )
    reflection_enabled: bool = field(default_factory=lambda: os.getenv("REFLECTION_ENABLED", "1") == "1")
    mood_enabled: bool = field(default_factory=lambda: os.getenv("MOOD_ENABLED", "1") == "1")
    life_enabled: bool = field(default_factory=lambda: os.getenv("LIFE_ENABLED", "1") == "1")
    life_tick_seconds: int = field(default_factory=lambda: int(os.getenv("LIFE_TICK_SECONDS", "900")))
    life_event_probability: float = field(
        default_factory=lambda: float(os.getenv("LIFE_EVENT_PROBABILITY", "0.5"))
    )
    nightly_time: str = field(default_factory=lambda: os.getenv("NIGHTLY_TIME", "23:30"))
    mood_baseline_valence: float = field(default_factory=lambda: float(os.getenv("MOOD_BASE_VALENCE", "0.44")))
    mood_baseline_arousal: float = field(default_factory=lambda: float(os.getenv("MOOD_BASE_AROUSAL", "0.38")))
    mood_baseline_energy: float = field(default_factory=lambda: float(os.getenv("MOOD_BASE_ENERGY", "0.70")))

    # 主动行为：戳一戳 / 空间说说
    proactive_enabled: bool = field(default_factory=lambda: os.getenv("PROACTIVE_ENABLED", "0") == "1")
    poke_enabled: bool = field(default_factory=lambda: os.getenv("POKE_ENABLED", "0") == "1")
    poke_target: int = field(default_factory=lambda: int(os.getenv("POKE_TARGET") or 0))
    poke_cooldown_seconds: int = field(
        default_factory=lambda: int(os.getenv("POKE_COOLDOWN_SECONDS", "10800"))
    )
    poke_daily_max: int = field(default_factory=lambda: int(os.getenv("POKE_DAILY_MAX", "3")))
    poke_idle_seconds: int = field(default_factory=lambda: int(os.getenv("POKE_IDLE_SECONDS", "5400")))
    poke_window_start: int = field(default_factory=lambda: int(os.getenv("POKE_WINDOW_START", "9")))
    poke_window_end: int = field(default_factory=lambda: int(os.getenv("POKE_WINDOW_END", "23")))
    poke_message_probability: float = field(
        default_factory=lambda: float(os.getenv("POKE_MESSAGE_PROBABILITY", "0.7"))
    )
    qzone_auto_feed_enabled: bool = field(
        default_factory=lambda: os.getenv("QZONE_AUTO_FEED_ENABLED", "0") == "1"
    )
    qzone_feed_daily_max: int = field(default_factory=lambda: int(os.getenv("QZONE_FEED_DAILY_MAX", "2")))
    qzone_feed_min_hours: int = field(default_factory=lambda: int(os.getenv("QZONE_FEED_MIN_HOURS", "3")))
    qzone_feed_impulse_probability: float = field(
        default_factory=lambda: float(os.getenv("QZONE_FEED_IMPULSE_PROBABILITY", "0.12"))
    )
    qzone_feed_chat_probability: float = field(
        default_factory=lambda: float(os.getenv("QZONE_FEED_CHAT_PROBABILITY", "0.15"))
    )
    qzone_feed_window_start: int = field(default_factory=lambda: int(os.getenv("QZONE_FEED_WINDOW_START", "10")))
    qzone_feed_window_end: int = field(default_factory=lambda: int(os.getenv("QZONE_FEED_WINDOW_END", "22")))

    # 安全 / 限速
    min_send_interval: float = field(default_factory=lambda: float(os.getenv("QB_MIN_SEND_INTERVAL", "1.0")))
    max_sends_per_minute: int = field(default_factory=lambda: int(os.getenv("QB_MAX_SENDS_PER_MINUTE", "90")))
    send_spacing: float = field(default_factory=lambda: float(os.getenv("QB_SEND_SPACING", "0.35")))
    send_soft_limit_per_minute: int = field(
        default_factory=lambda: int(os.getenv("QB_SEND_SOFT_LIMIT", "60"))
    )
    log_path: Path = field(default_factory=lambda: ROOT / "logs" / "qqbot.log")

    version: str = "0.1.0"


settings = Settings()
settings.db_path.parent.mkdir(parents=True, exist_ok=True)
settings.log_path.parent.mkdir(parents=True, exist_ok=True)
