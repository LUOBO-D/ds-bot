"""初始化 .env：从 .env.example 复制，并自动生成必需 token。

用法：python scripts/setup_env.py
"""

from __future__ import annotations

import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / ".env.example"
TARGET = ROOT / ".env"

GENERATE = ("ADMIN_TOKEN", "QZONE_ACCESS_TOKEN")


def new_token(name: str) -> str:
    if name == "ADMIN_TOKEN":
        return secrets.token_urlsafe(32)
    return secrets.token_hex(16)


def main() -> int:
    if not EXAMPLE.exists():
        print("[x] 找不到 .env.example，请确认在项目根目录运行")
        return 1
    text = TARGET.read_text(encoding="utf-8") if TARGET.exists() else EXAMPLE.read_text(encoding="utf-8")
    if not TARGET.exists():
        print("[+] 已从 .env.example 创建 .env")
    lines = text.splitlines()
    changed: list[str] = []
    filled: list[str] = []
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key not in GENERATE:
            continue
        value = line.split("=", 1)[1].strip() if "=" in line else ""
        if value:
            continue
        lines[index] = f"{key}={new_token(key)}"
        changed.append(key)
        filled.append(key)
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if filled:
        print(f"[+] 已生成 {', '.join(filled)}（保存在 .env，勿提交到仓库）")
    print(f"[=] 配置文件：{TARGET}")
    print("[i] 接下来编辑 .env，至少填好 DEEPSEEK_API_KEY，再运行 start.bat")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
