"""从 NapCat 重新取 QQ 空间 Cookie，写给 onebot-qzone sidecar。

用法：
    .venv\\Scripts\\python.exe scripts\\refresh_qzone_cookie.py
    .venv\\Scripts\\python.exe scripts\\refresh_qzone_cookie.py --restart

拿到的是当前登录号的 qzone Cookie（skey / p_skey / uin …），
写进 .qzone/cookies.json；sidecar 下次启动会直接读它，不用再扫码。

--restart 会在写完后尝试重启 sidecar（Windows 下用 taskkill 关掉 node 再拉起）。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qqbot.config import settings  # noqa: E402

COOKIE_PATH = ROOT / ".qzone" / "cookies.json"


def load_token() -> str:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("ADMIN_TOKEN="):
                return line.split("=", 1)[1].strip()
    return os.getenv("ADMIN_TOKEN", "")


def fetch_cookies(token: str) -> dict[str, str]:
    url = f"http://{settings.host}:{settings.port}/api/napcat/cookies"
    resp = httpx.get(
        url,
        params={"domain": "qzone.qq.com"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    raw = (data.get("raw") or {}).get("cookies") or ""
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            cookies[key.strip()] = value.strip()
    if not cookies:
        raise SystemExit("没有拿到 Cookie：NapCat 登录了吗？(GET /api/napcat/cookies)")
    return cookies


def write_cookies(cookies: dict[str, str]) -> None:
    COOKIE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_used": time.time(), "cookies": cookies}
    COOKIE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def restart_sidecar() -> None:
    ps = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -like '*onebot-qzone*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True,
        check=False,
    )
    subprocess.Popen(
        ["cmd.exe", "/c", "npm.cmd", "run", "dev"],
        cwd=str(ROOT / "services" / "onebot-qzone"),
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    print("sidecar 正在重启（新窗口），大约 10 秒后可用。")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--restart", action="store_true", help="写完后重启 sidecar")
    args = parser.parse_args()

    token = load_token()
    if not token:
        raise SystemExit("找不到 ADMIN_TOKEN，请先确认 .env 里有这一项。")

    cookies = fetch_cookies(token)
    write_cookies(cookies)
    keys = ", ".join(sorted(cookies)[:6])
    print(f"已写入 {COOKIE_PATH}（{len(cookies)} 个字段：{keys} …）")

    if args.restart:
        restart_sidecar()


if __name__ == "__main__":
    main()
