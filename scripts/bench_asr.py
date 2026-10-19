"""对真实的 QQ 语音样本跑 ASR，对比引擎 / 看识别效果。

用法（在项目根目录）：
    .\\.venv\\Scripts\\python.exe scripts\\bench_asr.py            # 两个引擎都跑
    .\\.venv\\Scripts\\python.exe scripts\\bench_asr.py sensevoice  # 只跑一个

样本来自 data/qqbot.db 里 direction='in' 且含 record 段的消息，
按 file 字段去 QQ 的 nt_data/Ptt 目录找原文件。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PTT_HINTS = [
    Path(os.getenv("VOICE_PTT_DIR", "path/to/nt_data/Ptt")),
]


def find_voice(name: str) -> Path | None:
    for base in PTT_HINTS:
        if not base.exists():
            continue
        hits = list(base.rglob(name))
        if hits:
            return hits[0]
    return None


def samples(limit: int = 6) -> list[tuple[str, Path]]:
    db = sqlite3.connect(ROOT / "data" / "qqbot.db")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT segments FROM messages WHERE direction='in' AND segments LIKE '%record%' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    out: list[tuple[str, Path]] = []
    for row in rows:
        for seg in json.loads(row["segments"] or "[]"):
            if seg.get("type") == "record":
                name = str((seg.get("data") or {}).get("file") or "")
                found = find_voice(name)
                if found:
                    out.append((name, found))
    return out


def main() -> None:
    engines = [sys.argv[1]] if len(sys.argv) > 1 else ["sensevoice", "paraformer"]
    items = samples()
    if not items:
        print("没有找到语音样本")
        return
    for engine in engines:
        os.environ["ASR_ENGINE"] = engine
        from qqbot.asr import Asr
        from qqbot.config import Settings

        asr = Asr(Settings())
        print(f"\n### {engine}  ({asr.active_dir})")
        if not asr.enabled:
            print("  未就绪")
            continue
        for name, src in items:
            try:
                t0 = time.perf_counter()
                text = asr._transcribe_path(src)
                cost = time.perf_counter() - t0
                print(f"  {cost:5.2f}s  {name[:12]}  {text}")
            except Exception as exc:  # noqa: BLE001
                print(f"  失败 {name[:12]}: {exc}")


if __name__ == "__main__":
    main()
