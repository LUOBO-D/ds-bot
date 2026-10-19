# 配置说明

所有配置都在项目根目录 `.env`（复制 `.env.example` 得到）。改完需重启 `run.py`。
`ADMIN_TOKEN` 留空会在首次启动时自动生成并写回 `.env`。

> 完整字段定义见 `qqbot/config.py`；下面按模块分组，只列常用项。

## 1. 服务本体

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QB_HOST` / `QB_PORT` | `127.0.0.1` / `3001` | 桥接监听地址；**不要改成 0.0.0.0 裸奔** |
| `ADMIN_TOKEN` | 自动生成 | REST 鉴权 Bearer Token |
| `QB_DB` | `data/qqbot.db` | SQLite 路径 |
| `QB_MIN_SEND_INTERVAL` | `1.0` | 两次发送的最小间隔（秒） |
| `QB_MAX_SENDS_PER_MINUTE` | `90` | 每分钟发送硬上限 |
| `QB_SEND_SPACING` | `0.35` | 平滑发送间隔 |
| `QB_SEND_SOFT_LIMIT` | `60` | 每分钟软上限（超出会排队） |
| `QB_CALL_TIMEOUT` | `15` | 单次 OneBot 调用超时 |
| `QB_SELF_ID` | `0` | 机器人 QQ 号，0 = 自动获取 |

## 2. 大脑（模型）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 任意 OpenAI 兼容服务的 key |
| `DEEPSEEK_BASE` | `https://api.deepseek.com` | 服务地址（含 `/v1` 的写法因供应商而异） |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 聊天模型 |
| `VISION_MODEL` | `deepseek-chat` | 识图模型（必须支持图片输入） |
| `QB_BRAIN_TIMEOUT` | `120` | 单次模型调用超时（秒） |
| `BRAIN_MAX_CONCURRENCY` | `4` | 同时进行的模型调用数 |
| `BRAIN_QUEUE_TIMEOUT` | `30` | 排队超时，超时放弃本轮回复 |
| `BRAIN_PRESENCE_PENALTY` | `0.4` | 防复读 |
| `BRAIN_FREQUENCY_PENALTY` | `0.3` | 防复读 |

## 3. 自动回复

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_REPLY_ENABLED` | `1` | 总开关 |
| `AUTO_REPLY_ALLOWLIST` | 空 | 空 = 不回复；`*`/`all`/`friends` = 所有好友；或逗号分隔 QQ 号 |
| `AUTO_REPLY_GAP` | `1.2` | 连续消息的静默去抖（秒） |
| `AUTO_REPLY_HISTORY` | `80` | 注入的历史条数（每条消息约 20~60 tokens，调大更连贯、更贵） |
| `AUTO_REPLY_STALE` | `90` | 超过该秒数的积压消息不再回复 |
| `REPLY_MULTI` | `1` | 允许分条发送 |
| `REPLY_MULTI_MAX` | `4` | 最多几条 |
| `REPLY_BUBBLE_DELAY_MIN/MAX` | `0.5`/`1.6` | 条与条之间的随机间隔 |
| `FOLLOWUP_ENABLED` | `1` | 回复后偶尔补一句 |
| `FOLLOWUP_PROBABILITY` | `0.3` | 补话概率 |
| `FOLLOWUP_DELAY_MIN/MAX` | `25`/`90` | 补话延迟区间（秒） |

## 4. 人格与记忆

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PERSONA_DEFAULT` | `xiaojingyu` | 默认角色卡 slug（`assets/personas/<slug>.md`） |
| `MEMORY_RECALL_K` | `12` | 每轮注入的事实条数 |
| `MEMORY_EVENTS_K` | `8` | 每轮注入的情节事件条数 |
| `MEMORY_DAYS_K` | `3` | 注入最近几天的全局日结 |
| `MEMORY_NOTES_K` | `3` | 注入几条「和你有关」的按人日结 |
| `MEMORY_MEMO_ENABLED` | `1` | 当天备忘开关 |
| `MEMORY_MEMO_MIN_MSGS` | `6` | 至少积累多少条新消息才压缩 |
| `MEMORY_MEMO_MIN_SECONDS` | `180` | 两次压缩的最小间隔 |
| `MEMORY_MEMO_MAX_MSGS` | `120` | 每批压缩的最大消息数 |
| `MEMORY_MEMO_MAX_CHARS` | `1200` | 当天备忘字数上限（超出截断；这是压缩整个白天的上限） |
| `REFLECTION_ENABLED` | `1` | 每轮对话后抽取事实/事件 |
| `MOOD_ENABLED` | `1` | 情绪系统 |
| `LIFE_ENABLED` | `1` | 生活线 |
| `LIFE_TICK_SECONDS` | `900` | 心跳间隔（秒） |
| `LIFE_EVENT_PROBABILITY` | `0.5` | 每拍推进生活事件的概率 |
| `NIGHTLY_TIME` | `23:30` | 每日日结时间 |
| `MOOD_BASE_VALENCE/AROUSAL/ENERGY` | `0.44/0.38/0.70` | 情绪基线 |

## 5. 识图 / 语音 / 表情包

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VISION_ENABLED` | `1` | 识图总开关 |
| `VISION_OCR_ENABLED` | `1` | 先用 NapCat `ocr_image` 抠文字，再让模型描述画面 |
| `VISION_MAX_BYTES` | `8 MB` | 超过就不下载 |
| `ASR_ENABLED` | `0` | 本地语音识别（需先下模型） |
| `ASR_ENGINE` | `sensevoice` | 主引擎；`paraformer` 可做兜底 |
| `ASR_THREADS` | `4` | CPU 线程数 |
| `ASR_CHUNK_SECONDS` | `30` | 长语音切块长度 |
| `STICKER_ENABLED` | `1` | 表情包库 |
| `STICKER_SEND_PROBABILITY` | `0.3` | 回复后顺手发表情包的概率 |
| `STICKER_SEND_COOLDOWN` | `900` | 两次发表情包的最小间隔（秒） |
| `STICKER_SEND_DAILY_MAX` | `8` | 每天最多发几个 |

## 6. 主动行为（默认关闭）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PROACTIVE_ENABLED` | `0` | 主动行为总开关 |
| `POKE_ENABLED` | `0` | 主动戳一戳 |
| `POKE_TARGET` | `0` | 戳谁（QQ 号），必须填 |
| `POKE_COOLDOWN_SECONDS` | `10800` | 两次戳之间至少间隔 |
| `POKE_DAILY_MAX` | `3` | 每天最多戳几次 |
| `POKE_IDLE_SECONDS` | `5400` | 对方至少静默多久才戳 |
| `POKE_WINDOW_START/END` | `9`/`23` | 允许戳的时间窗 |
| `POKE_MESSAGE_PROBABILITY` | `0.7` | 戳完再补一句的概率 |

## 7. QQ 空间与社交线（可选）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QZONE_BASE` | `http://127.0.0.1:5700` | onebot-qzone sidecar 地址 |
| `QZONE_ACCESS_TOKEN` | 空 | 必须与 sidecar 的 `ONEBOT_ACCESS_TOKEN` 一致 |
| `QZONE_AUTO_FEED_ENABLED` | `0` | 自主发说说 |
| `QZONE_FEED_DAILY_MAX` | `2` | 每天最多几条 |
| `QZONE_FEED_MIN_HOURS` | `3` | 两条之间至少隔几小时 |
| `QZONE_FEED_WINDOW_START/END` | `10`/`22` | 允许发的时间窗 |
| `QZONE_FEED_IMPULSE_PROBABILITY` | `0.12` | 心跳时「突然想发」的概率 |
| `QZONE_FEED_CHAT_PROBABILITY` | `0.15` | 聊完天后「想发」的概率 |
| `SOCIAL_ENABLED` | `1` | 刷好友动态 / 点赞 / 评论 |
| `SOCIAL_BROWSE_INTERVAL_SECONDS` | `1800` | 刷动态间隔 |
| `SOCIAL_BROWSE_DAILY_MAX` | `6` | 每天最多刷几次 |
| `SOCIAL_JUDGE_ENABLED` | `1` | 按自己喜好决定点不点赞 |
| `SOCIAL_JUDGE_MIN_INTEREST` | `0.5` | 兴趣低于该值不点赞 |
| `OWNER_UID` | `0` | 你自己的 QQ 号（区分「她的说说」与好友动态） |

## 8. 调参建议

- **想省钱**：把 `FOLLOWUP_ENABLED=0`、`MEMORY_MEMO_MIN_MSGS` 调大、`LIFE_TICK_SECONDS` 调大。
- **想更像人**：`BRAIN_PRESENCE_PENALTY` 保持 0.3–0.5，`REPLY_MULTI_MAX` 3–4，角色卡里别写死条数。
- **想更稳**：`AUTO_REPLY_ALLOWLIST` 只填自己，`PROACTIVE_ENABLED=0`，`BRAIN_MAX_CONCURRENCY=2`。
- **多人同时聊**：`BRAIN_MAX_CONCURRENCY` 4–8；发送侧由 `QB_*` 节流兜底，别调太快。
