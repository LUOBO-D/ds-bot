# 完整部署（有基础看这里）

> 目标：一台 Windows 电脑，从空目录跑到「QQ 里有人跟她聊天」，并能自己改配置、接别的模型。
> 全程约 20–30 分钟，其中 NapCat 安装 + 扫码最容易卡。
>
> **完全没装过 Python / 不想看原理？先看 [QUICKSTART.md](QUICKSTART.md)**——照着点鼠标 10 分钟能用上，再回来补这一份。

## 懒人路径（一键，和 QUICKSTART 一样）

1. `git clone https://github.com/LUOBO-D/ds-bot && cd ds-bot`
2. 双击 `install.bat`：建环境、装依赖、生成 `.env`、下载 NapCat 安装器
3. 双击 `napcat-onekey\NapCatInstaller.exe` 装好 QQ + NapCat
4. 记事本打开 `.env`，填 `DEEPSEEK_API_KEY`（和 `AUTO_REPLY_ALLOWLIST`）
5. 双击 `start.bat`；扫码登录小号；在 WebUI 加反向 WS `ws://127.0.0.1:3001/onebot/v11/ws`
6. 用大号私聊小号，能看到 `gateway connected` 就成功了

下面是从零手动的详细步骤，遇到问题时对照。

## 0. 前置清单

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| 系统 | Windows 10/11 | — |
| Python | 3.10+（实测 3.14） | `python --version` |
| Git | 任意较新版本 | `git --version` |
| QQ | 官方 NT 客户端（**必须 64 位**，见 Q1） | — |
| QQ 小号 | 至少一个，最好有好友可测试 | — |
| 大模型 Key | DeepSeek 或任意 OpenAI 兼容服务 | — |

> ⚠️ 不要用主号。个人号接协议端有风控/封号风险，用专用小号。

---

## 1. 克隆项目、装依赖

```powershell
git clone https://github.com/LUOBO-D/ds-bot.git
cd ds-bot
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

如果 `python` 指向 Microsoft Store 的占位程序，用 `py -3 -m venv .venv` 代替。

---

## 2. 安装 NapCat 并登录小号

NapCat 的官方安装方式有两种，任选：

### 方式 A：OneKey 安装器（推荐）

1. 打开 <https://github.com/NapNeko/NapCatQQ/releases>，下载 `NapCat.OneKey.exe`（或 `NapCatInstaller.exe`）。
2. 运行，安装器会**自建一份独立的 64 位 QQ**（不动你现有的 QQ）。
3. 安装完成后启动，用手机 QQ 扫码登录小号。

### 方式 B：Shell 版 + 已有 64 位 QQ

1. 下载 `NapCat.Shell.zip` 解压。
2. 确认你的 QQ 是 64 位：任务管理器 → 详细信息 → `QQ.exe` 是否带 `(32 位)`。
   32 位 QQ 下 NapCat 注入会静默失败（见 [Q1](#q1-napcat-启动了但-6099-面板打不开也没有配置文件)）。
3. 用 Shell 自带的 `launcher.bat` 启动（需要管理员权限）。

> 登录成功标志：NapCat 控制台出现 `WebUi Token: xxxxx`，
> 并且 `http://127.0.0.1:6099` 能打开 WebUI。

---

## 3. 配置反向 WebSocket

1. 打开 NapCat WebUI：`http://127.0.0.1:6099/webui?token=<控制台里的 token>`
2. 进入 **网络配置 → WebSocket 客户端 → 添加**
3. 填：

   | 字段 | 值 |
   | --- | --- |
   | 名称 | `local-bridge` |
   | URL | `ws://127.0.0.1:3001/onebot/v11/ws` |
   | 消息格式 | `array` |
   | 上报自身消息 | 关闭 |
   | Token | 留空（除非你在 `.env` 配了 `OB_ACCESS_TOKEN`） |

4. 保存。NapCat 会立刻尝试连接；桥接没启动时它会每 3 秒重连，属正常。

> 也可以直接改 `napcat/config/onebot11_<QQ号>.json` 里的 `websocketClients`，**改完要重启 NapCat 才生效**。

---

## 4. 配置 `.env`

```powershell
copy .env.example .env
notepad .env
```

最少改这两项：

```ini
DEEPSEEK_API_KEY=sk-你的key
AUTO_REPLY_ALLOWLIST=10001     # 你自己的 QQ 号；* = 所有好友
```

说明：

- `ADMIN_TOKEN` 留空会自动生成并写回 `.env`。
- `AUTO_REPLY_ALLOWLIST` 留空 = **只收消息不回复**，适合先观察日志。
- 想接入别家模型：见 README 的「接入你自己的 API」。

---

## 5. 启动

```powershell
.\start.ps1
# 等价于：
# $env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe run.py
```

正常日志：

```
自动回复已启用 allowlist=[10001] gap=1.2s model=deepseek-chat
gateway connected (self_id=<你的小号>)
Uvicorn running on http://127.0.0.1:3001
```

验证（PowerShell）：

```powershell
$t = (Select-String -Path .env -Pattern '^ADMIN_TOKEN=(.+)$').Matches.Groups[1].Value
Invoke-RestMethod -Uri http://127.0.0.1:3001/api/state `
  -Headers @{ Authorization = "Bearer $t" } | ConvertTo-Json -Depth 4
```

`gateway.connected = true` 就通了。用你的 QQ 私聊机器人小号，发一句「在吗」试试。

---

## 6. 可选模块

### 6.1 QQ 空间（发说说 / 看动态 / 点赞评论）

空间能力不来自 NapCat，而是一个独立 sidecar：

```powershell
git clone https://github.com/Gu-Heping/onebot-qzone.git services/onebot-qzone
cd services/onebot-qzone
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1; npm install
```

在 `services/onebot-qzone/.env` 写：

```ini
ONEBOT_HOST=127.0.0.1
ONEBOT_PORT=5700
ONEBOT_ACCESS_TOKEN=<随便一串随机字符，和主 .env 的 QZONE_ACCESS_TOKEN 一致>
QZONE_CACHE_PATH=../.qzone
QZONE_ENABLE_QR=0
QZONE_PLAYWRIGHT_CHANNEL=msedge
```

启动：`npm run dev`，然后回到主项目：

1. 启动桥接（需要 NapCat 已登录）
2. 取 Cookie：`GET /api/napcat/cookies?domain=qzone.qq.com`，返回 `raw.cookies` 字符串
3. 交给 sidecar：`POST /api/qzone/cookie`，body 为 `{"cookie": "<raw.cookies>"}`
   （如果接口不可用，也可以手动写 `.qzone/cookies.json`：
   `{ "last_used": 1700000000, "cookies": { "...": "..." } }`）
4. 重启 sidecar，日志出现「检测到已缓存 Cookie，已登录」即成功；`GET /api/qzone/status` 可查状态

> 二维码登录也可用（`POST /api/qzone/qr`），但 Playwright 扫码超时 300 秒后会退出，所以推荐 Cookie 注入。

### 6.2 本地语音识别（听语音）

1. 下载 SILK 解码器：<https://github.com/kn007/silk-v3-decoder/releases>，
   把 `silk_v3_decoder.exe` 放到 `tools/silk/`。
2. 下载 SenseVoice int8 模型（约 158 MB）：
   <https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models>
   解压到 `models/sense-voice/`，目录里应有 `model.int8.onnx` 与 `tokens.txt`。
3. `.env` 改 `ASR_ENABLED=1`，重启。
4. 验证：`POST /api/asr/transcribe {"file": "<一条 .amr 语音的路径>"}`

### 6.3 表情包库

内置 `assets/stickers/manifest.json`（QQ 自带 face 表情）。好友发来的图片表情会进库，
无需额外安装；想手动加，把图片放进 `data/inbox/` 再调用 `POST /api/stickerbook/judge`。

---

## 7. 排错

### Q1: NapCat 启动了但 6099 面板打不开，也没有配置文件

最常见原因是 **QQ 是 32 位**：NapCat 的 `NapCatWinBootHook.dll` 是 x64，注入 x86 QQ 会静默失败。
任务管理器看 `QQ.exe` 是否 32 位；是的话换 64 位 QQ，或直接用 OneKey 安装器（会装独立 64 位 QQ）。

### Q2: 桥接起来了，但 `gateway.connected = false`

- NapCat 那边「WebSocket 客户端」地址是不是 `ws://127.0.0.1:3001/onebot/v11/ws`
- 消息格式是不是 `array`
- 改过 `onebot11_<QQ号>.json` 的话，NapCat 需要重启才生效
- 端口被占用：`netstat -ano | findstr :3001`，杀掉残留的 `python.exe`

### Q3: `[Errno 10048] error while attempting to bind on address`

上一次的 `run.py` 没退干净：

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*run.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

注意：后台任务管理器里 `kill` 掉包装进程不一定会杀掉 python 子进程，要按上面查。

### Q4: 控制台中文乱码

`start.ps1` 已设置 `PYTHONUTF8=1`。如果手动启动，先设：

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'
```

PowerShell 里用 `Invoke-RestMethod` 读 UTF-8 JSON 可能显示乱码，用 `python -m json.tool` 或浏览器看。

### Q5: 回复很慢 / 超时

- 第一次调用要加载模型、建连接，1–3 秒正常
- 查 `/api/traffic`（并发、发送节流）
- 模型供应商限流时，调小 `BRAIN_MAX_CONCURRENCY`

### Q6: 收不到图片 / 表情包的描述

- `VISION_ENABLED=1` 且 `VISION_MODEL` 是支持图片的模型
- 看 `/api/images` 里有没有记录；图片下载失败通常是 rkey 过期（收到后要尽快处理）

### Q7: 语音识别一直是空的

- `tools/silk/silk_v3_decoder.exe` 是否存在
- `models/sense-voice/model.int8.onnx` 是否存在
- `GET /api/asr` 看 `enabled` 与 `last_error`
