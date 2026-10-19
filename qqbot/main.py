"""qqbot 服务入口。

一个 uvicorn 进程同时提供：
- 反向 WS 入口（NapCat 连进来）：`/onebot/v11/ws`
- 手动驱动 REST API：`/api/*`（Swagger: `/docs`）
- 事件流 SSE：`/api/events`
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, model_validator

from . import personas
from .config import settings
from .onebot import OneBotClient, OneBotError, event_to_record
from .autoreply import AutoReply
from .brain import Brain, BrainError
from .emotion import Mood
from .keeper import Keeper
from .life import LifeService
from .memory import MemoryService
from .proactive import Proactive
from .qzone import QzoneClient, QzoneError
from .security import limiter, require_token
from .social import Social
from .stickers import StickerBook
from .store import Store
from .vision import Vision, _ocr_text
from .asr import Asr
from .tools import Actions, list_stickers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(settings.log_path, encoding="utf-8")],
)
log = logging.getLogger("qqbot")

store = Store(settings.db_path)
ob = OneBotClient()
actions = Actions(ob, store)
qz = QzoneClient()
brain = Brain()
mood = Mood(store)
memory = MemoryService(store, brain, mood)
life = LifeService(store, brain, mood)
vision = Vision(store, settings, ocr=actions.ocr)
asr = Asr(settings)
proactive = Proactive(store, actions, brain=brain, mood=mood, life=life, qzone=qz)
social = Social(store, qz, life=life, mood=mood, onebot=ob, brain=brain)
stickerbook = StickerBook(store, brain, actions, vision=vision, mood=mood)
keeper = Keeper(store, mood, life, memory, proactive=proactive, social=social)
autoreply = AutoReply(
    ob, store, actions, brain, memory=memory, mood=mood, life=life,
    proactive=proactive, vision=vision, social=social, stickers=stickerbook, asr=asr,
)
subscribers: set[asyncio.Queue[dict[str, Any]]] = set()


async def _broadcast(event: dict[str, Any]) -> None:
    for queue in list(subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass


async def _consume_events() -> None:
    while True:
        event = await ob.events.get()
        try:
            record = event_to_record(event, self_id=ob.self_id)
            if record:
                store.add_message(**record)
                log.info(
                    "message %s %s %s: %s",
                    record["message_type"],
                    record["group_id"] or record["user_id"],
                    record["sender_name"] or record["user_id"],
                    (record["raw_message"] or "")[:120],
                )
            await _broadcast(event)
        except Exception:
            log.exception("event consumer failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_consume_events())
    if settings.autoreply_enabled and settings.deepseek_api_key:
        autoreply.start()
    if settings.deepseek_api_key:
        keeper.start()
    log.info("qqbot v%s listening on %s:%s (ws %s)", settings.version, settings.host, settings.port, settings.ws_path)
    try:
        yield
    finally:
        task.cancel()
        await keeper.stop()
        await autoreply.stop()
        await brain.close()
        await vision.close()
        await asr.close()
        await qz.close()


app = FastAPI(
    title="qqbot",
    version=settings.version,
    lifespan=lifespan,
    description=(
        "QQ 消息 / 表情包 / 空间能力的手动驱动服务。\n\n"
        "鉴权：`Authorization: Bearer <ADMIN_TOKEN>`（.env 中自动生成）。\n"
        "NapCat 反向 WS 地址：`/onebot/v11/ws`。"
    ),
)

Actor = Annotated[str, Header(alias="X-Actor", description="调用者标识，写入审计日志")]

try:
    settings.sticker_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/stickers", StaticFiles(directory=str(settings.sticker_dir)), name="stickers")
except Exception:  # 目录异常不影响主服务
    log.exception("挂载表情库目录失败")


# ---------------------------------------------------------------- 模型

class Target(BaseModel):
    type: Literal["group", "private"]
    id: int


class SendBody(BaseModel):
    target: Target
    segments: list[Any] | None = None
    text: str | None = None
    reply_to: int | None = None

    @model_validator(mode="after")
    def _require_content(self) -> "SendBody":
        if not self.segments and not self.text:
            raise ValueError("segments 与 text 至少提供一个")
        return self


class TextBody(BaseModel):
    target: Target
    text: str
    reply_to: int | None = None


class ImageBody(BaseModel):
    target: Target
    file: str


class VisionBody(BaseModel):
    url: str | None = None
    file: str | None = None
    kind: str | None = None
    sticker: bool = False


class StickerJudgeBody(BaseModel):
    url: str | None = None
    file: str | None = None
    desc: str | None = None
    sender_id: int | None = None
    message_id: int | None = None


class StickerSendBody(BaseModel):
    user_id: int = 0
    sticker_id: int | None = None


class StickerBody(BaseModel):
    target: Target
    face_id: int | None = None
    file: str | None = None
    sticker_id: str | None = None
    tag: str | None = None


class PokeBody(BaseModel):
    target: Target


class EmojiLikeBody(BaseModel):
    message_id: int
    emoji_id: str = "76"


class QzonePostBody(BaseModel):
    text: str
    images: list[str] = []


class QzoneCommentBody(BaseModel):
    tid: str
    target_uin: int
    content: str
    reply_comment_id: str | None = None
    reply_uin: int | None = None


class QzoneLikeBody(BaseModel):
    tid: str
    user_id: int | None = None


class QzoneCookieBody(BaseModel):
    cookie: str


class ChatBody(BaseModel):
    text: str
    user_name: str = ""
    user_id: int = 0
    multi: bool | None = None


class BrainToggleBody(BaseModel):
    enabled: bool


class MemoryBody(BaseModel):
    content: str
    kind: str = "fact"
    user_id: int | None = None


class MoodBody(BaseModel):
    valence: float | None = None
    arousal: float | None = None
    energy: float | None = None
    note: str | None = None


class ReflectBody(BaseModel):
    user_id: int = 0
    text: str
    reply: str = ""
    user_name: str = ""


# ---------------------------------------------------------------- 工具

async def _audited(actor: str, action: str, params: Any, factory) -> Any:
    try:
        result = await factory()
    except (OneBotError, QzoneError) as exc:
        store.audit(actor, action, params, {"error": str(exc)}, False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        store.audit(actor, action, params, {"error": str(exc)}, False)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    store.audit(actor, action, params, result, True)
    return result


def _check_rate(target: Target) -> None:
    limiter.check(f"{target.type}:{target.id}")


# ---------------------------------------------------------------- 基础

@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse("/docs")


@app.get("/api/health", tags=["基础"])
async def health():
    return {
        "ok": True,
        "version": settings.version,
        "gateway": {
            "connected": ob.connected,
            "self_id": ob.self_id,
            "last_event_at": ob.last_event_at,
        },
        "db": store.stats(),
    }


@app.get("/api/state", tags=["基础"], dependencies=[Depends(require_token)])
async def state() -> dict[str, Any]:
    return {
        "gateway": ob.status(),
        "db": store.stats(),
        "qzone": {"base": settings.qzone_base},
        "mind": {
            "mood": mood.snapshot(),
            "keeper": keeper.stats,
            "memory": memory.stats,
            "life": life.stats,
        },
        "vision": {**vision.stats, "enabled": vision.enabled, "model": settings.vision_model},
        "asr": asr.status(),
        "sse_subscribers": len(subscribers),
        "limits": {
            "min_send_interval": settings.min_send_interval,
            "max_sends_per_minute": settings.max_sends_per_minute,
        },
    }


@app.get("/api/traffic", tags=["运维"], dependencies=[Depends(require_token)])
async def traffic() -> dict[str, Any]:
    """多人同时聊时的负载与限流视图。"""
    return {
        "brain": {
            "inflight": brain.inflight,
            "max_inflight": brain.max_inflight,
            "max_concurrency": settings.brain_max_concurrency,
            "queue_timeout": settings.brain_queue_timeout,
        },
        "send": {
            "sent_total": actions.sent_total,
            "waited_total_ms": actions.waited_total_ms,
            "spacing": settings.send_spacing,
            "soft_limit_per_minute": settings.send_soft_limit_per_minute,
        },
        "replies": {
            "received": autoreply.stats.get("received"),
            "replied": autoreply.stats.get("replied"),
            "skipped": autoreply.stats.get("skipped"),
            "errors": autoreply.stats.get("errors"),
            "pending_users": len(autoreply._pending),
            "pending_user_ids": sorted(autoreply._pending),
            "last_latency_ms": autoreply.stats.get("last_latency_ms"),
        },
    }


@app.get("/api/social", tags=["社交"], dependencies=[Depends(require_token)])
async def social_status() -> dict[str, Any]:
    return social.status()


@app.get("/api/social/feeds", tags=["社交"], dependencies=[Depends(require_token)])
async def social_feeds(limit: int = 20) -> dict[str, Any]:
    return {"items": store.feeds(limit=limit, persona=personas.active())}


class FeedInteractBody(BaseModel):
    tid: str
    uin: int | None = None
    content: str | None = None


async def _feed_row(body: FeedInteractBody) -> dict[str, Any]:
    row = store.feed_by_tid(body.tid)
    if row:
        return row
    return {"tid": body.tid, "uin": body.uin or 0, "nickname": "", "content": ""}


class SocialTextBody(BaseModel):
    text: str


@app.post("/api/social/interact", tags=["社交"], dependencies=[Depends(require_token)])
async def social_interact(body: SocialTextBody, actor: Actor = "api") -> dict[str, Any]:
    notes = await social.interact(body.text)
    store.audit(actor, "social.interact", {"text": body.text}, {"notes": notes}, True)
    return {"ok": bool(notes), "notes": notes}


@app.post("/api/social/judge", tags=["社交"], dependencies=[Depends(require_token)])
async def social_judge(body: FeedInteractBody) -> dict[str, Any]:
    feed = await _feed_row(body)
    return await social.judge(feed, force=True)


@app.post("/api/social/like", tags=["社交"], dependencies=[Depends(require_token)])
async def social_like(body: FeedInteractBody, actor: Actor = "api") -> dict[str, Any]:
    result = await social.like_feed(await _feed_row(body))
    store.audit(actor, "social.like", body.model_dump(), result, bool(result.get("ok")))
    return result


@app.post("/api/social/comment", tags=["社交"], dependencies=[Depends(require_token)])
async def social_comment(body: FeedInteractBody, actor: Actor = "api") -> dict[str, Any]:
    feed = await _feed_row(body)
    text = body.content or await social._think_comment(feed)
    if not text:
        return {"ok": False, "error": "没有评论内容"}
    result = await social.comment_feed(feed, text)
    store.audit(actor, "social.comment", {"tid": body.tid, "content": text}, result, bool(result.get("ok")))
    return result


@app.post("/api/social/browse", tags=["社交"], dependencies=[Depends(require_token)])
async def social_browse(force: bool = True, num: int = 8, actor: Actor = "api") -> dict[str, Any]:
    result = await social.browse(num=num, force=force)
    store.audit(actor, "social.browse", {"num": num, "force": force}, result, bool(result.get("ok")))
    return result


@app.get("/api/images", tags=["识图"], dependencies=[Depends(require_token)])
async def images(limit: int = 50) -> dict[str, Any]:
    return {"items": store.images(limit=limit), "stats": vision.stats, "enabled": vision.enabled}


@app.post("/api/vision/ocr", tags=["识图"], dependencies=[Depends(require_token)])
async def vision_ocr(body: VisionBody) -> dict[str, Any]:
    if not body.file:
        raise HTTPException(status_code=400, detail="需要 file")
    result = await actions.ocr(body.file)
    return {"ok": True, "result": result, "text": _ocr_text(result)}


@app.post("/api/vision/describe", tags=["识图"], dependencies=[Depends(require_token)])
async def vision_describe(body: VisionBody) -> dict[str, Any]:
    if body.url:
        text = await vision.describe_url(body.url, kind=body.kind or "image")
    elif body.file:
        text = await vision.describe_file(body.file, sticker=body.sticker)
    else:
        raise HTTPException(status_code=400, detail="需要 url 或 file")
    return {"ok": bool(text), "summary": text or "", "stats": vision.stats}


@app.get("/api/asr", tags=["语音"], dependencies=[Depends(require_token)])
async def asr_status() -> dict[str, Any]:
    return asr.status()


@app.post("/api/asr/transcribe", tags=["语音"], dependencies=[Depends(require_token)])
async def asr_transcribe(body: VisionBody) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if body.file:
        text, meta = await asr.transcribe_file_meta(body.file)
    elif body.url:
        text = await asr.transcribe_url(body.url)
    else:
        raise HTTPException(status_code=400, detail="需要 file 或 url")
    return {"ok": bool(text), "text": text, "meta": meta, "stats": asr.stats}


@app.get("/api/brain", tags=["大脑"], dependencies=[Depends(require_token)])
async def brain_status() -> dict[str, Any]:
    return {
        "ready": brain.ready,
        "enabled": autoreply.enabled,
        "model": brain.model,
        "allowlist": sorted(autoreply.allowlist),
        "allowlist_all_friends": autoreply.allowlist_all_friends,
        "friends_cached": len(autoreply._friends),
        "inflight": brain.inflight,
        "max_inflight": brain.max_inflight,
        "max_concurrency": settings.brain_max_concurrency,
        "gap": settings.autoreply_gap,
        "history": settings.autoreply_history,
        "persona": personas.active(),
        "persona_name": personas.name(),
        "pending": {str(k): v["parts"] for k, v in autoreply._pending.items()},
        "stats": autoreply.stats,
    }


@app.post("/api/brain/enabled", tags=["大脑"], dependencies=[Depends(require_token)])
async def brain_set_enabled(body: BrainToggleBody, actor: Actor = "api") -> dict[str, Any]:
    autoreply.enabled = body.enabled
    store.audit(actor, "brain.enabled", body.model_dump(), {"enabled": autoreply.enabled}, True)
    log.info("自动回复已%s", "启用" if autoreply.enabled else "关闭")
    return {"enabled": autoreply.enabled}


@app.post("/api/chat", tags=["大脑"], dependencies=[Depends(require_token)])
async def chat(body: ChatBody) -> dict[str, Any]:
    """只调大脑、不发 QQ，用来试人设（带完整记忆/心情/生活线上下文）。"""
    ctx = autoreply._context(body.user_id, body.text)
    try:
        out = await brain.chat(
            user_text=body.text,
            user_name=body.user_name,
            multi=settings.reply_multi if body.multi is None else body.multi,
            multi_max=settings.reply_multi_max,
            **ctx,
        )
    except BrainError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {
        "reply": out["text"],
        "bubbles": out.get("bubbles"),
        "usage": out.get("usage"),
        "elapsed_ms": out.get("elapsed_ms"),
        "context": ctx,
    }


@app.post("/api/chat/send", tags=["大脑"], dependencies=[Depends(require_token)])
async def chat_send(body: ChatBody, actor: Actor = "api") -> dict[str, Any]:
    """完整走一遍：读历史 → 调大脑 → 发到指定 QQ（默认小萝卜）。"""
    try:
        result = await autoreply.simulate(body.user_id, body.text, body.user_name)
    except BrainError as exc:
        store.audit(actor, "chat.send", body.model_dump(), {"error": str(exc)}, False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.audit(actor, "chat.send", body.model_dump(), {"reply": result["reply"]}, True)
    return result


@app.get("/api/memory", tags=["大脑"], dependencies=[Depends(require_token)])
async def memory_list(
    user_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    return {"memories": store.memories(user_id=user_id, limit=limit, persona=personas.active())}


@app.post("/api/memory", tags=["大脑"], dependencies=[Depends(require_token)])
async def memory_add(body: MemoryBody, actor: Actor = "api") -> dict[str, Any]:
    memory_id = store.add_memory(
        body.content, kind=body.kind, user_id=body.user_id, persona=personas.active()
    )
    store.audit(actor, "memory.add", body.model_dump(), {"id": memory_id}, True)
    return {"id": memory_id, "content": body.content}


@app.delete("/api/memory/{memory_id}", tags=["大脑"], dependencies=[Depends(require_token)])
async def memory_delete(memory_id: int, actor: Actor = "api") -> dict[str, Any]:
    ok = store.delete_memory(memory_id)
    store.audit(actor, "memory.delete", {"id": memory_id}, {"ok": ok}, ok)
    if not ok:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return {"ok": True}


class PersonaBody(BaseModel):
    persona: str


@app.get("/api/personas", tags=["角色"], dependencies=[Depends(require_token)])
async def personas_list() -> dict[str, Any]:
    return {"active": personas.active(), "items": personas.listing()}


@app.post("/api/personas", tags=["角色"], dependencies=[Depends(require_token)])
async def personas_switch(body: PersonaBody, actor: Actor = "api") -> dict[str, Any]:
    """切换角色卡：下一轮对话立即生效，记忆/情绪按角色隔离。"""
    slug = personas.resolve(body.persona)
    if not slug:
        raise HTTPException(status_code=404, detail="没有这张角色卡")
    personas.set_active(slug)
    mood.reload()
    store.audit(actor, "persona.switch", body.model_dump(), {"slug": slug}, True)
    log.info("切换角色 -> %s", slug)
    return {"ok": True, "active": slug, "name": personas.name(slug)}


@app.get("/api/mind", tags=["心灵"], dependencies=[Depends(require_token)])
async def mind() -> dict[str, Any]:
    """一眼看她的内在状态：心情、心跳、记忆、生活线。"""
    return {
        "persona": {"slug": personas.active(), "name": personas.name()},
        "mood": mood.snapshot(),
        "keeper": keeper.status(),
        "memory": memory.stats,
        "life": life.status(),
        "proactive": proactive.status(),
        "social": social.status(),
        "stickers": stickerbook.status(),
        "db": store.stats(),
    }


@app.get("/api/mood", tags=["心灵"], dependencies=[Depends(require_token)])
async def mood_get() -> dict[str, Any]:
    return mood.snapshot()


@app.post("/api/mood", tags=["心灵"], dependencies=[Depends(require_token)])
async def mood_set(body: MoodBody, actor: Actor = "api") -> dict[str, Any]:
    mood.set(valence=body.valence, arousal=body.arousal, energy=body.energy, note=body.note)
    store.audit(actor, "mood.set", body.model_dump(), mood.snapshot(), True)
    return mood.snapshot()


@app.get("/api/memory/events", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_events(
    user_id: int | None = None,
    limit: int = Query(30, ge=1, le=500),
) -> dict[str, Any]:
    return {"events": store.events(user_id=user_id, limit=limit, persona=personas.active())}


@app.delete("/api/memory/events/{event_id}", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_event_delete(event_id: int, actor: Actor = "api") -> dict[str, Any]:
    ok = store.delete_event(event_id)
    store.audit(actor, "event.delete", {"id": event_id}, {"ok": ok}, ok)
    if not ok:
        raise HTTPException(status_code=404, detail="情节不存在")
    return {"ok": True}


@app.get("/api/memory/days", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_days(limit: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    return {"days": store.day_summaries(limit=limit, persona=personas.active())}


@app.post("/api/memory/reflect", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_reflect(body: ReflectBody, actor: Actor = "api") -> dict[str, Any]:
    """手动触发一次反思（抽事实/事件 + 调心情）。"""
    try:
        out = await memory.reflect(body.user_id, body.text, body.reply, name=body.user_name)
    except BrainError as exc:
        store.audit(actor, "memory.reflect", body.model_dump(), {"error": str(exc)}, False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.audit(actor, "memory.reflect", {"user_id": body.user_id}, out, True)
    return out


@app.get("/api/memory/memo", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_memo(uid: int = Query(...), day: str | None = None) -> dict[str, Any]:
    """当天和这个人的聊天备忘（滚动压缩版）。"""
    target = day or life.today()
    return {
        "day": target,
        "uid": uid,
        "memo": store.day_memo(uid, day=target, persona=personas.active()),
    }


@app.post("/api/memory/memo", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_memo_update(
    uid: int = Query(...), force: bool = True, actor: Actor = "api"
) -> dict[str, Any]:
    """手动把今天的对话压进当天备忘。"""
    try:
        out = await memory.update_memo(uid, name=str(uid), force=force)
    except BrainError as exc:
        store.audit(actor, "memory.memo", {"uid": uid}, {"error": str(exc)}, False)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    store.audit(actor, "memory.memo", {"uid": uid, "force": force}, out, bool(out.get("ok")))
    return out


@app.post("/api/memory/nightly", tags=["心灵"], dependencies=[Depends(require_token)])
async def memory_nightly(day: str | None = None, uid: int | None = None, actor: Actor = "api") -> dict[str, Any]:
    target = day or life.today()
    out = await memory.nightly(target, uid=uid)
    store.audit(actor, "memory.nightly", {"day": target}, out, bool(out.get("summary")))
    return out


@app.get("/api/life", tags=["心灵"], dependencies=[Depends(require_token)])
async def life_get(day: str | None = None, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    target = day or life.today()
    return {
        "day": target,
        "plan": life.plan_of(target),
        "log": store.life_log(day=target, limit=limit, persona=personas.active()),
        "stats": life.stats,
    }


@app.post("/api/life/tick", tags=["心灵"], dependencies=[Depends(require_token)])
async def life_tick(force: bool = True, actor: Actor = "api") -> dict[str, Any]:
    out = await life.tick(force=force)
    store.audit(actor, "life.tick", {"force": force}, out, not out.get("error"))
    return out


@app.get("/api/keeper", tags=["心灵"], dependencies=[Depends(require_token)])
async def keeper_status() -> dict[str, Any]:
    return keeper.status()


@app.post("/api/keeper/tick", tags=["心灵"], dependencies=[Depends(require_token)])
async def keeper_tick(force: bool = False, actor: Actor = "api") -> dict[str, Any]:
    out = await keeper.tick(force=force)
    store.audit(actor, "keeper.tick", {"force": force}, out, True)
    return out


@app.get("/api/proactive", tags=["心灵"], dependencies=[Depends(require_token)])
async def proactive_status() -> dict[str, Any]:
    """主动行为的开关、频率限制与最近动作。"""
    return proactive.status()


@app.post("/api/proactive/poke", tags=["心灵"], dependencies=[Depends(require_token)])
async def proactive_poke(
    force: bool = True,
    with_message: bool | None = None,
    actor: Actor = "api",
) -> dict[str, Any]:
    """立刻戳一戳（测试/手动）。force=True 时无视冷却和时段。"""
    out = await proactive.poke(reason="manual", force=force, with_message=with_message)
    store.audit(actor, "proactive.poke", {"force": force}, out, bool(out.get("ok")))
    return out


@app.post("/api/proactive/feed", tags=["心灵"], dependencies=[Depends(require_token)])
async def proactive_feed(force: bool = True, actor: Actor = "api") -> dict[str, Any]:
    """立刻问她要不要发一条说说（force=True 只跳过时段/频次限制，仍由她判断）。"""
    out = await proactive.feed(reason="manual", force=force)
    store.audit(actor, "proactive.feed", {"force": force}, out, bool(out.get("ok")))
    return out


@app.post("/api/proactive/feed/preview", tags=["心灵"], dependencies=[Depends(require_token)])
async def proactive_feed_preview(reason: str = "manual") -> dict[str, Any]:
    """只做「想不想发 / 写什么」的判断，不发布（未登录也能测）。"""
    return await proactive.preview_feed(reason)


@app.get("/api/account", tags=["基础"], dependencies=[Depends(require_token)])
async def account() -> dict[str, Any]:
    return {
        "login": await ob.get_login_info(),
        "status": await ob.get_status(),
    }


@app.get("/api/friends", tags=["查询"], dependencies=[Depends(require_token)])
async def friends(no_cache: bool = False) -> Any:
    return await ob.get_friend_list(no_cache=no_cache)


@app.get("/api/groups", tags=["查询"], dependencies=[Depends(require_token)])
async def groups(no_cache: bool = False) -> Any:
    return await ob.get_group_list(no_cache=no_cache)


@app.get("/api/groups/{group_id}/members", tags=["查询"], dependencies=[Depends(require_token)])
async def group_members(group_id: int) -> Any:
    return await ob.get_group_member_list(group_id)


@app.get("/api/groups/{group_id}/history", tags=["查询"], dependencies=[Depends(require_token)])
async def group_history(group_id: int, count: int = Query(20, ge=1, le=100)) -> Any:
    return await actions.history(group_id=group_id, count=count)


@app.get("/api/messages", tags=["查询"], dependencies=[Depends(require_token)])
async def messages(
    group_id: int | None = None,
    user_id: int | None = None,
    direction: Literal["in", "out"] | None = None,
    keyword: str | None = None,
    before_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    rows = store.messages(
        group_id=group_id,
        user_id=user_id,
        direction=direction,
        keyword=keyword,
        before_id=before_id,
        limit=limit,
    )
    return {"count": len(rows), "messages": rows}


@app.get("/api/audit", tags=["查询"], dependencies=[Depends(require_token)])
async def audit(limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    rows = store.audits(limit)
    return {"count": len(rows), "audits": rows}


@app.get("/api/stickers", tags=["表情包"], dependencies=[Depends(require_token)])
async def stickers(tag: str | None = None) -> dict[str, Any]:
    items = list_stickers(tag)
    return {"count": len(items), "tag": tag, "items": items}


@app.get("/api/stickerbook", tags=["表情包"], dependencies=[Depends(require_token)])
async def stickerbook_list(limit: int = 50) -> dict[str, Any]:
    items = store.stickers(limit=limit)
    return {"status": stickerbook.status(), "count": len(items), "items": items}


@app.post("/api/stickerbook/judge", tags=["表情包"], dependencies=[Depends(require_token)])
async def stickerbook_judge(body: StickerJudgeBody) -> dict[str, Any]:
    return await stickerbook.consider(
        url=body.url or "",
        path=body.file,
        desc=body.desc or "",
        sender_id=body.sender_id,
        message_id=body.message_id,
    )


@app.post("/api/stickerbook/send", tags=["表情包"], dependencies=[Depends(require_token)])
async def stickerbook_send(body: StickerSendBody) -> dict[str, Any]:
    if body.sticker_id is None:
        got = await stickerbook.maybe_send(int(body.user_id), force=True)
        return {"ok": bool(got), "sticker": got}
    rows = [item for item in store.stickers(limit=settings.sticker_max) if int(item["id"]) == int(body.sticker_id)]
    if not rows:
        raise HTTPException(status_code=404, detail="sticker not found")
    item = rows[0]
    result = await actions.send_sticker("private", int(body.user_id), file=str(item["path"]))
    store.touch_sticker(int(item["id"]))
    return {"ok": True, "result": result, "sticker": item}


@app.delete("/api/stickerbook/{sticker_id}", tags=["表情包"], dependencies=[Depends(require_token)])
async def stickerbook_delete(sticker_id: int) -> dict[str, Any]:
    return {"ok": store.delete_sticker(sticker_id)}


@app.get("/stickers/{name}", include_in_schema=False)
async def sticker_file(name: str) -> FileResponse:
    path = (settings.sticker_dir / name).resolve()
    if not str(path).startswith(str(settings.sticker_dir.resolve())) or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path)


# ---------------------------------------------------------------- 发送

@app.post("/api/send", tags=["发送"], dependencies=[Depends(require_token)])
async def send(body: SendBody, actor: Actor = "api") -> Any:
    _check_rate(body.target)
    segments = body.segments or ([{"type": "text", "data": {"text": body.text}}] if body.text else [])
    return await _audited(
        actor,
        "send",
        body.model_dump(),
        lambda: actions.send(body.target.type, body.target.id, segments, reply_to=body.reply_to),
    )


@app.post("/api/send/text", tags=["发送"], dependencies=[Depends(require_token)])
async def send_text(body: TextBody, actor: Actor = "api") -> Any:
    _check_rate(body.target)
    return await _audited(
        actor,
        "send_text",
        body.model_dump(),
        lambda: actions.send_text(body.target.type, body.target.id, body.text, reply_to=body.reply_to),
    )


@app.post("/api/send/image", tags=["发送"], dependencies=[Depends(require_token)])
async def send_image(body: ImageBody, actor: Actor = "api") -> Any:
    _check_rate(body.target)
    return await _audited(
        actor,
        "send_image",
        body.model_dump(),
        lambda: actions.send_image(body.target.type, body.target.id, body.file),
    )


@app.post("/api/send/sticker", tags=["表情包"], dependencies=[Depends(require_token)])
async def send_sticker(body: StickerBody, actor: Actor = "api") -> Any:
    _check_rate(body.target)
    return await _audited(
        actor,
        "send_sticker",
        body.model_dump(),
        lambda: actions.send_sticker(
            body.target.type,
            body.target.id,
            face_id=body.face_id,
            file=body.file,
            sticker_id=body.sticker_id,
            tag=body.tag,
        ),
    )


@app.post("/api/poke", tags=["发送"], dependencies=[Depends(require_token)])
async def poke(body: PokeBody, actor: Actor = "api") -> Any:
    _check_rate(body.target)
    return await _audited(
        actor, "poke", body.model_dump(), lambda: actions.poke(body.target.type, body.target.id)
    )


@app.post("/api/emoji/like", tags=["发送"], dependencies=[Depends(require_token)])
async def emoji_like(body: EmojiLikeBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor,
        "emoji_like",
        body.model_dump(),
        lambda: actions.emoji_like(body.message_id, body.emoji_id),
    )


@app.post("/api/recall", tags=["发送"], dependencies=[Depends(require_token)])
async def recall(message_id: int, actor: Actor = "api") -> Any:
    return await _audited(actor, "recall", {"message_id": message_id}, lambda: actions.recall(message_id))


# ---------------------------------------------------------------- 事件流

@app.get("/api/events", tags=["事件"], dependencies=[Depends(require_token)])
async def events(request: Request) -> StreamingResponse:
    async def stream():
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        subscribers.add(queue)
        try:
            yield f"event: hello\ndata: {json.dumps({'ok': True, 'self_id': ob.self_id})}\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event.get('post_type', 'event')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            subscribers.discard(queue)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- QQ 空间

@app.get("/api/qzone/status", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_status() -> dict[str, Any]:
    out: dict[str, Any] = {"reachable": False, "base": settings.qzone_base}
    try:
        out["login"] = await qz.login_info()
        out["status"] = await qz.bridge_status()
        out["reachable"] = True
    except QzoneError as exc:
        out["error"] = str(exc)
    return out


@app.post("/api/qzone/cookie", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_cookie(body: QzoneCookieBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor, "qzone.cookie", {"cookie": "***"}, lambda: qz.login_cookie(body.cookie)
    )


@app.post("/api/qzone/qr", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_qr(actor: Actor = "api") -> Any:
    return await _audited(actor, "qzone.qr", {}, lambda: qz.login_qr())


@app.get("/api/napcat/cookies", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def napcat_cookies(domain: str = "qzone.qq.com") -> Any:
    """从 NapCat 读取指定域名的 Cookie（免扫码打通 QQ 空间）。"""
    raw = await ob.call("get_cookies", domain=domain)
    return {"domain": domain, "raw": raw}


@app.post("/api/qzone/post", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_post(body: QzonePostBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor, "qzone.post", body.model_dump(), lambda: qz.publish(body.text, body.images)
    )


@app.get("/api/qzone/posts", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_posts(
    user_id: int | None = None,
    pos: int = 0,
    num: int = Query(10, ge=1, le=30),
    include_image_data: bool = False,
) -> Any:
    return await qz.posts(user_id=user_id, pos=pos, num=num, include_image_data=include_image_data)


@app.get("/api/qzone/feeds", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_feeds(num: int = Query(10, ge=1, le=30), include_image_data: bool = False) -> Any:
    return await qz.friend_feeds(num=num, include_image_data=include_image_data)


@app.post("/api/qzone/comment", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_comment(body: QzoneCommentBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor,
        "qzone.comment",
        body.model_dump(),
        lambda: qz.comment(
            target_uin=body.target_uin,
            tid=body.tid,
            content=body.content,
            reply_comment_id=body.reply_comment_id,
            reply_uin=body.reply_uin,
        ),
    )


@app.post("/api/qzone/like", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_like(body: QzoneLikeBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor, "qzone.like", body.model_dump(), lambda: qz.like(tid=body.tid, user_id=body.user_id)
    )


@app.post("/api/qzone/unlike", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_unlike(body: QzoneLikeBody, actor: Actor = "api") -> Any:
    return await _audited(
        actor, "qzone.unlike", body.model_dump(), lambda: qz.unlike(tid=body.tid, user_id=body.user_id)
    )


@app.post("/api/qzone/delete", tags=["QQ空间"], dependencies=[Depends(require_token)])
async def qzone_delete(tid: str, actor: Actor = "api") -> Any:
    return await _audited(actor, "qzone.delete", {"tid": tid}, lambda: qz.delete_post(tid))


# ---------------------------------------------------------------- 反向 WS

@app.websocket(settings.ws_path)
async def onebot_ws(websocket: WebSocket) -> None:
    if settings.ob_token:
        token = websocket.query_params.get("access_token")
        if token != settings.ob_token:
            await websocket.close(code=1008)
            return
    await websocket.accept()
    self_id = websocket.query_params.get("self_id") or websocket.headers.get("x-self-id")
    await ob.attach(websocket, self_id)
    try:
        while True:
            raw = await websocket.receive_text()
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                log.warning("非法 WS 报文: %s", raw[:200])
                continue
            if isinstance(payload, dict):
                await ob.handle(payload)
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("WS 会话异常")
    finally:
        await ob.detach(websocket)
