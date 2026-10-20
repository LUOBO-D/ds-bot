# 主动私聊 + 配图（v1）

目标：让她自己想起他、主动发一条消息，并在发之前先准备好一张「随手拍」的图，图和话一起发出去。（早安/晚安仪式已于 2026-10-20 按用户要求删除，只保留自发私聊。）

## 1. 决策：什么时候才发

心跳（`Keeper` 每 15 分钟）调用 `Proactive.tick()`，其中新加两类动作：

| 动作 | 触发 | 默认限制 |
| --- | --- | --- |
| 自发私聊 `send_message()` | 7:00–24:00，且他至少 90 分钟没说话、距上次主动 ≥4 小时、当天未发过 | 每天最多 1 条 |

硬护栏（任一满足就不发）：

- 上一条主动消息还没被回 → 先不追（`unanswered`）
- 他最近还在说话（< `PROACTIVE_MESSAGE_IDLE_SECONDS`）→ 不打扰
- 当天额度用完 / 冷却未到 / 不在时段

意愿打分（`_motivation()`）：基础 0.35 + 空闲时长（最多 +0.3）+ 心情低落（最多 +0.2）+ 精力低（+0.05）+ 今天生活线有事（+0.1），与 `PROACTIVE_MESSAGE_THRESHOLD`（默认 0.55）比较后才发；`force=true` 跳过所有判断（仅手动接口用）。

## 2. 文案 + 画面（一次模型调用）

`MESSAGE_PROMPT` 让她输出 JSON：

```json
{"text": "不超过 30 字的第一人称消息", "scene": "只拍场景/物件、不出现人的画面描述"}
```

`text` 直接发；`scene` 交给文生图。

## 3. 生图（`qqbot/imagegen.py`）

**当前默认：智谱 CogView-3-Flash（免费）**

- 接口：`POST https://open.bigmodel.cn/api/paas/v4/images/generations`，`model=cogview-3-flash`，`size=864x1152`；
- 返回图片 URL 后立刻下载（URL 会过期），转 JPEG（约 75 KB），实测 8–12 秒；
- 智谱在右下角有「AI生成」水印，出图后统一裁掉底部 8%；
- 失败自动重试（429/5xx 退避重试，最多 3 次）；仍失败返回 `None` → 这条消息**只发文字**。

**备选 provider**：`IMAGEGEN_PROVIDER=pollinations`（免 key，Flux，每 IP 45 秒一张，会偶发 402/超时）。切换只改 `.env`，不用改代码。

支持情况：智谱 `cogview-3-flash`（免费）、通义万相（约 0.14 元/张）、硅基流动 Kolors/FLUX（约 0.02–0.05 元/张）——后两家加 `_fetch_xxx()` 即可。

## 4. 发送顺序

1. 图片（`image` 段，`file:///` 本地路径）
2. 文字（1 条）
3. 写 `life_log`（"我给他发了张照片，说：…"）、写审计、扣当天图片额度

## 5. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/proactive` | 含 `message` / `imagegen` 状态 |
| POST | `/api/proactive/message?force=true&dry_run=true` | 只生成文案+图片，不发送 |
| GET | `/api/imagegen` | 生图统计与最近错误 |

## 5.5 聊天里也能顺手拍一张

回复时她自己判断要不要配图：想要就另起一行写 [[IMG: 画面描述]]，这行不会发出去，会变成一张图（先说话、图随后到）。

- 只对私聊生效；群聊不配图。
- 每天最多 `IMAGE_REPLY_DAILY_MAX=20` 张，同一个人两次之间至少 `IMAGE_REPLY_MIN_INTERVAL=300` 秒，避免刷图。
- **自拍**：她可以在回复里写 `[[自拍: 在做什么/在哪/什么表情]]`，按角色设定图（蓝发、蕾丝发带、鲸鱼尾巴）出图；开关 `IMAGE_SELFIE_ENABLED=1`。自拍和场景图共用同一个每日额度。
- 出图失败静默跳过，不影响文字回复。
- 提示词约束：只拍物件/场景，不出现人、脸、手、文字。

## 6. 开关

`.env`：

```
PROACTIVE_MESSAGE_ENABLED=1
PROACTIVE_MESSAGE_TARGET=10001   # 换成你自己的 QQ
PROACTIVE_MESSAGE_DAILY_MAX=1
PROACTIVE_MESSAGE_WINDOW_START=7
PROACTIVE_MESSAGE_WINDOW_END=24
PROACTIVE_MESSAGE_IDLE_SECONDS=5400
PROACTIVE_MESSAGE_COOLDOWN_SECONDS=14400
PROACTIVE_MESSAGE_THRESHOLD=0.55
IMAGEGEN_ENABLED=1
IMAGEGEN_PROVIDER=zhipu
IMAGEGEN_MODEL=cogview-3-flash
IMAGEGEN_API_KEY=<智谱 API Key>
IMAGEGEN_BASE=https://open.bigmodel.cn/api/paas/v4
IMAGEGEN_SIZE=864x1152
IMAGEGEN_STYLE=anime
IMAGEGEN_MAX_PER_DAY=1
IMAGE_REPLY_ENABLED=1
IMAGE_REPLY_DAILY_MAX=20
IMAGE_REPLY_MIN_INTERVAL=300
```

## 7. 已知边界

- 免费生图有队列和限流，出图 5–60 秒不等；发送会等图好了再一起发，所以偶发慢。
- 只对 `PROACTIVE_MESSAGE_TARGET`（默认 owner）生效；不对好友群发。
- 深夜（0:00–7:00）不发，即使用户在。

## 双引擎出图（2026-10-20）

| 场景 | 引擎 | 单价 | 说明 |
| --- | --- | --- | --- |
| 随手拍/场景图 | 智谱 `cogview-3-flash` | 免费 | 纯文生图，不带人物 |
| 自拍（带人物） | 阿里云百炼 `wan2.7-image` | ¥0.2/张 | 用 `assets/personas/xiaojingyu-appearance.png` 当参考图，保持角色一致 |

配置：`IMAGEGEN_SELFIE_PROVIDER=dashscope`、`DASHSCOPE_API_KEY`、`IMAGEGEN_WAN_MODEL=wan2.7-image`、`IMAGEGEN_WAN_SIZE=2K`；自拍失败会自动回退到 CogView 的纯文字描述出图。

### 引擎自动判断（2026-10-20）

- 回复里写 `[[自拍: …]]` → 百炼 wan2.7-image（带参考图）
- 回复里写 `[[IMG: …]]`，但场景描述里出现「我/自拍/镜子里的我」或「她坐在…」这类她本人入镜的说法 → 也走百炼
- 其余 `[[IMG: …]]` → 免费 CogView
- 判断用的正则：`qqbot/autoreply.py::_SELF_SCENE_RE`；命中会写日志「配图引擎：…」
