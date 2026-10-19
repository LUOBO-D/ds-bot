"""OneBot 11 消息段构造、归一化与人类可读描述。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Sequence

Segment = dict[str, Any]

ROOT = Path(__file__).resolve().parent.parent


def seg(type_: str, **data: Any) -> Segment:
    return {"type": type_, "data": {k: v for k, v in data.items() if v is not None}}


def text(content: str) -> Segment:
    return seg("text", text=content)


def at(qq: int | str) -> Segment:
    return seg("at", qq=str(qq))


def at_all() -> Segment:
    return seg("at", qq="all")


def reply(message_id: int | str) -> Segment:
    return seg("reply", id=str(message_id))


def face(face_id: int | str) -> Segment:
    return seg("face", id=str(face_id))


def mface(emoji_id: str, emoji_package_id: str | int, key: str, summary: str = "[表情包]") -> Segment:
    return seg(
        "mface",
        emoji_id=str(emoji_id),
        emoji_package_id=str(emoji_package_id),
        key=key,
        summary=summary,
    )


def local_file_token(path: str | Path) -> str:
    """本地绝对路径 → NapCat 认的 file:/// 形式；不存在则原样返回（URL / base64）。"""
    raw = str(path)
    p = Path(raw)
    if not p.is_absolute():
        p = ROOT / raw
    if p.exists():
        posix = p.as_posix()
        if posix.startswith("/"):
            return f"file://{posix}"
        return f"file:///{posix}"
    return raw


def image(file: str | Path, sub_type: int | None = None) -> Segment:
    return seg("image", file=local_file_token(file), sub_type=sub_type)


def sticker_image(file: str | Path) -> Segment:
    """sub_type=1 让 QQ 以「自定义表情包」而不是普通图片渲染。"""
    return seg("image", file=local_file_token(file), sub_type=1)


def record(file: str | Path) -> Segment:
    return seg("record", file=local_file_token(file))


def video(file: str | Path) -> Segment:
    return seg("video", file=local_file_token(file))


def file_(file: str | Path, name: str | None = None) -> Segment:
    return seg("file", file=local_file_token(file), name=name)


def json_card(data: str | dict) -> Segment:
    return seg("json", data=data)


def music(type_: str, id_: str | None = None, url: str | None = None, audio: str | None = None) -> Segment:
    return seg("music", type=type_, id=id_, url=url, audio=audio)


def forward(forward_id: str) -> Segment:
    return seg("forward", id=str(forward_id))


def poke(target_id: int | str) -> Segment:
    return seg("poke", type="poke", id=str(target_id))


def dice() -> Segment:
    return seg("dice")


def rps() -> Segment:
    return seg("rps")


def node(user_id: int | str, nickname: str, content: Sequence[Segment]) -> dict[str, Any]:
    return {"type": "node", "data": {"user_id": str(user_id), "nickname": nickname, "content": list(content)}}


def normalize(items: Iterable[Any] | None) -> list[Segment]:
    """把宽松输入统一成 OneBot 消息段数组。

    允许：字符串 / {"type","data"} / {"text": ...} / {"image": ...} / {"at": ...} / {"face": ...}
    """
    if items is None:
        return []
    out: list[Segment] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            if item:
                out.append(text(item))
            continue
        if not isinstance(item, dict):
            out.append(text(str(item)))
            continue
        if "type" in item and isinstance(item.get("data"), dict):
            out.append({"type": str(item["type"]), "data": dict(item["data"])})
            continue
        if "type" in item:
            data = {k: v for k, v in item.items() if k != "type"}
            out.append({"type": str(item["type"]), "data": data})
            continue
        if "text" in item:
            out.append(text(str(item["text"])))
            continue
        if "image" in item:
            out.append(image(item["image"]))
            continue
        if "sticker" in item:
            out.append(sticker_image(item["sticker"]))
            continue
        if "face" in item:
            out.append(face(item["face"]))
            continue
        if "at" in item:
            out.append(at(item["at"]))
            continue
        if "reply" in item:
            out.append(reply(item["reply"]))
            continue
        raise ValueError(f"无法识别的消息段: {item!r}")
    return out


_LABELS = {
    "face": "[表情]",
    "mface": "[表情包]",
    "image": "[图片]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "forward": "[合并转发]",
    "json": "[卡片]",
    "music": "[音乐]",
    "poke": "[戳一戳]",
    "dice": "[骰子]",
    "rps": "[猜拳]",
    "reply": "[引用]",
    "at": "@",
}


def describe(items: Iterable[Any] | None) -> str:
    """把消息段压成一行人类可读文本，用于日志和 SSE 预览。"""
    parts: list[str] = []
    for item in items or []:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        type_ = item.get("type", "?")
        data = item.get("data") or {}
        if type_ == "text":
            parts.append(str(data.get("text", "")))
        elif type_ == "at":
            qq = str(data.get("qq", ""))
            parts.append("@全体成员" if qq == "all" else f"@{qq}")
        elif type_ == "face":
            parts.append(f"[表情{data.get('id', '')}]")
        elif type_ == "image":
            parts.append("[表情包]" if str(data.get("sub_type")) == "1" else "[图片]")
        else:
            parts.append(_LABELS.get(type_, f"[{type_}]"))
    return "".join(parts).strip()
