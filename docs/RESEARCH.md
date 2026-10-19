# QQ 机器人生态调研（2026-10-19）

> 目标：让本地 AI 助手具备「收发 QQ 消息 / 发表情包 / 操作 QQ 空间」的能力。
> 本文只记录结论与证据，不含实现代码。

---

## 0. 一句话结论

**NapCat（QQ NT 协议端）+ OneBot 11 + 自建桥接服务** 是当前唯一「活跃维护 + 功能全 + 能碰 QQ 空间」的组合。
QQ 空间没有官方开放接口，社区唯一新鲜可用的实现是 `Gu-Heping/onebot-qzone`（今天还在提交）。

---

## 1. 生态全景

| 项目 | ★ | 语言 | 最近提交 | 定位 | 结论 |
|---|---|---|---|---|---|
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | 42.1k | Python | 2026-10-18 | LLM 机器人框架，多平台 | 生态最大，但它是「另一个大脑」，不是我们要的零件 |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 6.3k | Python | 2026-10-18 | 赛博群友人设 | 参考其「群聊不刷屏」策略 |
| [mirai](https://github.com/mamoe/mirai) | 14.8k | Kotlin | **2024-09-23** | 老一代协议库 | **已停更**，签名服务器生态崩了，不要用 |
| [NapCatQQ](https://github.com/NapNeko/NapCatQQ) | 11.0k | TypeScript | 2026-10-15 | NTQQ 协议端，OneBot 11 | **首选网关**（最新 v4.18.38，2026-10-12） |
| [NoneBot2](https://github.com/nonebot/nonebot2) | 7.8k | Python | 2026-10-18 | 异步机器人框架 | 可选上层框架，插件多 |
| [Koishi](https://github.com/koishijs/koishi) | 6.3k | TypeScript | 2026-10-08 | 跨平台框架 | 可选，TS 生态 |
| [LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot)（原 LLOneBot） | 3.7k | TypeScript | 2026-10-12 | OneBot11/Satori/Milky | NapCat 的备选，需 LiteLoader |
| [Lagrange.Core](https://github.com/LagrangeDev/Lagrange.Core) | 3.0k | C# | 2026-09-07 | 纯 C# NTQQ 协议 | 无需装 QQ 客户端，但要签名服务 |
| [NcatBot](https://github.com/ncatbot/NcatBot) | 118 | Python | 2026-10-04 | NapCat 的 Python SDK | 写插件时可抄它的封装 |
| [go-cqhttp](https://github.com/Mrs4s/go-cqhttp) | — | Go | 归档 | 上一代 | **已死**，别用 |
| [onebot-qzone](https://github.com/Gu-Heping/onebot-qzone) | 15 | TypeScript | 2026-10-19 | QQ 空间 ↔ OneBot | **空间唯一新鲜方案** |

**淘汰项**：mirai / go-cqhttp（停更，签名服务失效）。选它们会在登录环节直接卡死。

---

## 2. 网关选型：为什么是 NapCat

**工作方式**：NapCat 不是重写协议，而是给官方 QQ NT 客户端做注入（Windows 上依赖已安装的 QQNT 9.9.x），把客户端能力翻译成 OneBot 11 的 HTTP/WebSocket 接口。
本机检测到 `C:\Program Files (x86)\Tencent\QQNT\QQ.exe`，走这条路线零额外成本。

对比：

| | NapCat | Lagrange.Core | LLOneBot |
|---|---|---|---|
| 需要装 QQ 客户端 | 是 | 否 | 是（+LiteLoader） |
| 需要签名服务器 | 否 | **是** | 否 |
| 更新频率 | 极高 | 中 | 高 |
| 空间扩展 | 有插件机制 | 无 | 无 |

代价：**账号风险高于官方 bot 平台**（见 §6）。

---

## 3. 协议能力（OneBot 11 + NapCat 扩展）

### 3.1 网络模型

- HTTP：`POST /send_group_msg` 这种一问一答。
- **反向 WebSocket（推荐）**：NapCat 作为客户端连到我们的服务，双向复用一条连接——事件推送 + API 调用都在上面，实时性最好。
  注意 NapCat WebUI 里的命名：选「WebSocket 客户端」，填我们的 `ws://127.0.0.1:3001/onebot/v11/ws`。
- 资源统一用 `base64://`、`file://`、本地路径、`http(s)://`、`data:` 这几种写法。

### 3.2 消息段（能发什么）

| 类型 | 用途 | 关键字段 |
|---|---|---|
| `text` | 文本 | `text` |
| `at` | @人 / @全体 | `qq`（`all` 表示全体） |
| `reply` | 引用回复 | `id` |
| `face` | QQ 内置表情 | `id`（如 14=微笑、178=斜眼笑） |
| `mface` | 商城表情包 | `emoji_id`、`emoji_package_id`、`key`、`summary` |
| `image` | 图片 / **自定义表情包** | `file`、`sub_type`（**1 = 以表情包形式发送**） |
| `record` | 语音（silk） | `file` |
| `video` / `file` | 视频 / 文件 | `file`、`name` |
| `json` | 卡片消息 | `data` |
| `music` | 音乐分享 | `type` + `id`/`url` |
| `forward` | 合并转发 | `id` |
| `poke` | 戳一戳 | `type`、`id` |
| `dice` / `rps` | 骰子 / 猜拳 | 无需字段 |

> 表情包有两条路：内置 `face` 用 ID 表；自定义图用 `image` + `sub_type=1`，QQ 侧会渲染成表情包而不是普通图片。

### 3.3 NapCat 扩展 API（官方 OneBot 没有的）

- 账号：`set_self_longnick`（签名）、`set_qq_profile`、`set_online_status`、`set_input_status`（"正在输入"）
- 消息：`set_msg_emoji_like`（给消息加表情回应）、`get_group_msg_history`、`get_friend_msg_history`、`ocr_image`（图里认字）、`fetch_custom_face`（拿自定义表情）
- 互动：`group_poke` / `friend_poke` / `send_poke`、`send_like`（资料卡点赞）
- 转发：`send_group_forward_msg` / `send_private_forward_msg`
- 群管理：禁言、踢人、设管理、群公告、精华消息、群文件全套
- **凭证：`get_cookies(domain)` + `get_csrf_token`** ← QQ 空间能打通的关键（见 §4）

### 3.4 硬限制（会影响设计）

- NapCat **无数据库**，消息 ID 走 LRU，约 5000 条后过期；**撤回的消息无法再取**。
  → 需要留痕的内容必须我们自己落库。
- 同一个 QQ 号不能在多端同时用同一套 OneBot 网关做「收消息」（会互相抢）。

---

## 4. QQ 空间：唯一可行路线

**官方没有个人号空间 API**。所有方案都是逆向 Web API：

- 鉴权：Cookie 里的 `uin` / `skey` / `p_skey`，请求带 `g_tk`（由 `p_skey` 算哈希）。
- 发说说：`emotion_cgi_publish_v6`（图片/视频走同一套上传）。
- 读说说：`feeds3_html_more`（PC 的 `emotion_cgi_msglist_v6` 实测常返回 `-10000`）。
- 互动：评论/点赞/转发各有一套 CGI，且有降级链。

**可复用项目**：

| 项目 | 说明 |
|---|---|
| [onebot-qzone](https://github.com/Gu-Heping/onebot-qzone) | **首选**。40 个 action，OneBot 风格 HTTP/WS，发布/删除/点赞/评论/转发/相册/访客/隐私/图片 base64，含「首次启动基线」防历史事件轰炸、Cookie 10 分钟保活、请求指纹随机化 |
| [aioqzone](https://github.com/aioqzone/aioqzone) | Python 封装，偏读取 |
| [astrbot_plugin_qzone](https://github.com/Zhalslar/astrbot_plugin_qzone) | AstrBot 插件，`internal_dolike_app` 点赞链 |
| [nonebot-adapter-qzone](https://github.com/qzqzcsclub/nonebot-adapter-qzone) | NoneBot 适配器，扫码登录流程可抄 |

**已知坑**（来自其 README）：
- 评论只能从 feeds3 HTML 里解析，三天前的帖子可能拿不到评论详情；
- 点赞列表是 best-effort，和真实计数会不一致；
- 好友列表默认是「最近动态里出现过的人」，要全量得开浏览器抓；
- Cookie 过期返回 `1401`，需要重新扫码/贴 Cookie。

**衔接点**：NapCat 的 `get_cookies` 能直接拿到同一账号的 Cookie，理论上可以自动喂给 onebot-qzone 的 `login_cookie`，免二次扫码（待实测）。

---

## 5. 表情包体系

- **发**：内置表情用 `face`（ID 表），自定义图用 `image + sub_type=1`，商城表情用 `mface`（需要 `emoji_id`/`emoji_package_id`/`key`，蹭现成的 key 才发得出去）。
- **收**：商城表情会转成 `image` 并附带 `emoji_id`/`emoji_package_id`/`key`。
  → 可以把群友发的表情自动攒成本地「表情库」，以后复用（这是最自然的表情来源）。
- **互动**：`set_msg_emoji_like` 给消息贴表情回应；`poke` 戳一戳；`dice`/`rps` 可以玩。

---

## 6. 风险与合规（必须提前说清）

1. **封号风险**：NapCat 走真实客户端，比协议库低，但仍属「非官方 bot」，频率高/内容敏感/被举报会封。建议先用小号，控速、控内容。
2. **空间更敏感**：批量点赞/评论、频繁发说说最容易触发风控。
3. **官方替代**：QQ 开放平台（`q.qq.com`）的官方 bot 合规稳定，但**不能操作空间**、不能加任意群、能力受审核约束。两者只能二选一或并存两个号。
4. **隐私**：消息内容会被发往模型 API，需要明确哪些群/人允许。

---

## 7. 推荐架构

```
        ┌────────────────┐   反向 WS（事件↓ / API↑）
        │  QQ NT + NapCat │◄──────────────────────────┐
        └────────────────┘                            │
                                                      │
  ┌──────────────┐   HTTP(5700)   ┌───────────────────┴──┐
  │ onebot-qzone │◄──────────────►│   qqbot 桥接服务      │
  │  (空间 sidecar)│               │  · 事件路由/去重      │
  └──────────────┘                │  · 人设+记忆(SQLite)  │
                                  │  · 模型调用(DeepSeek) │
                                  │  · 工具: 发消息/表情包 │
                                  │  · 管理 REST(127.0.0.1)│
                                  └───────────┬──────────┘
                                              │ 本地 REST
                                     ┌────────┴─────────┐
                                     │ 我这个会话/你手动  │
                                     └──────────────────┘
```

- 端口规划：`3001` 反向 WS、`3002` 管理 REST（仅本机+token）、`6099` NapCat WebUI、`5700` 空间 sidecar。
- 群聊默认「不刷屏」：只有被 @ / 前缀 / 私聊才回；连续对话有冷却。
- 所有对外动作带限速与审计日志（谁让发的、发了什么）。

---

## 8. 待定决策

1. 用哪个号当机器人（小号 / 本机已有的大号）？
2. 语言栈：Python（生态大、迭代快）还是 TypeScript（与 NapCat/空间 sidecar 同栈）？
3. 大脑：接 DeepSeek API 常驻自动回复，还是先只做「我能手动驱动」？
4. 部署：就这台 Windows 电脑常驻，还是之后要迁到服务器/群晖？
