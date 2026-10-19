# 心智层设计：记忆 / 情绪 / 生活线

> v1，2026-10-19 上线。目标不是「更聪明的回复」，而是让沈知微在时间上连贯：她记得、她有自己的日子、她的情绪会漂。

## 1. 为什么这样分

| 层 | 回答的问题 | 存储 | 生命周期 |
| --- | --- | --- | --- |
| L0 工作记忆 | 刚才在聊什么 | `messages` 最近 N 条 | 30 条滚动 |
| L0.5 当天备忘 | 今天一整天都聊了什么 | `day_memo` 滚动压缩 | 按天，随对话增量更新 |
| L1 情节记忆 | 我们之间发生过什么 | `events` 事件卡 | 长期 |
| L2 语义记忆 | 关于他，我知道什么 | `memories` 事实 | 长期，带权重 |
| L3 生活线 | 她今天在过什么日子 | `life_log` + `day_summary` | 按天 |

补充：`memories` 同时承担「显式记住」（用户说「记住…」）和「反思提炼」（事后由模型总结），用 `kind` 与 `source` 区分。

## 2. 数据表

```sql
memories(id, at, kind, user_id, content, source, salience, last_used_at, weight)
events(id, at, user_id, kind, summary, emotion, salience, source, source_msg_id, mood_delta)
life_log(id, at, day, kind, content, mood, tags)
day_summary(day PK, at, summary, mood, highlights)
day_memo(day, user_id, persona PK, memo, last_msg_id, msg_count, updated_at)
day_notes(day, user_id PK, summary, mood, highlights, persona)
```

- `kind`：`explicit` / `fact` / `event` / `preference`
- `source`：`chat`（显式记住）/ `reflection`（反思）/ `manual`
- `salience` 0–1，`weight` 随被召回次数上升（越常用越靠前），`last_used_at` 用于新鲜度排序。

## 3. 情绪（`qqbot/emotion.py`）

三维：`valence`（愉悦）/ `arousal`（唤醒）/ `energy`（精力），外加一句 `note`（为什么）。

- 基线：0.44 / 0.38 / 0.70
- 每 tick 向基线回落（`DECAY_PER_TICK=0.18`），情绪是「会好起来的」而不是永久状态
- 来源：反思模型给出 `mood_delta`（-1..1），按 `scale` 衰减后叠加，并写一句中文 note
- 注入方式：`prompt_block()` 告诉模型「现在的心情、要真实体现在语气和长短上、别用数值说话」

## 4. 记忆（`qqbot/memory.py`）

- `terms(text)`：中文 2-gram + ASCII 词，用于 LIKE 命中
- `recall(uid, text, k)`：SQLite `memories` 关键词打分（命中数 × salience + weight），另取最近 `events` 8 条 + 昨天/前天的 `day_summary`
- `reflect(uid, user_text, reply, name, message_id)`：每条回复后异步跑一次，让模型输出 JSON：
  - `facts`（关于他的长期事实）、`event`（今天这件小事）、`mood_delta`
  - 落库到 `memories` / `events`，并更新 `mood`
- `update_memo(uid, name, force)`：**当天备忘**。把今天新聊的消息（每批 120 条，从早到晚推进）增量压进 `day_memo.memo`，保留仍然有效的内容、删掉过时的；同一人同一天只有一条，`last_msg_id` 记住压到哪条消息。触发：每条回复后异步一次（`MEMORY_MEMO_MIN_SECONDS=180` 限流、`MEMORY_MEMO_MIN_MSGS=6` 起步），日结前再强制补一次
- `memo_block(uid)`：把当天备忘注入聊天上下文，让她「记得这一整天」而不是只记得最近 30 条
- `nightly(day, uid=...)`：23:30 把当天事件压成一段「她视角」的日记式摘要；按人日结优先吃当天备忘（已压缩过的对话），写 `day_notes`

## 5. 生活线（`qqbot/life.py`）

- `ensure_plan(day)`：每天第一次心跳时生成日程
  - `mood_start`（醒来时的心情）、`plan`（3–5 条今天要做的事）、`intention`（心里挂着的一件事）
- `tick()`：15 分钟一拍，45% 概率推进一个小事件（正在做的事 / 路上的见闻 / 想到的事），深夜静默
- `context_block(day, limit=3)`：把「今天醒来的样子 + 日程 + 心里挂着的 + 今天已发生的事」拼成一段，注入对话
- 不发主动消息：生活线只影响她说话时自然带出的内容

## 6. 心跳（`qqbot/keeper.py`）

`LIFE_TICK_SECONDS=900` 的后台任务：

1. `mood.decay()`
2. `life.tick()`
3. 到点（`NIGHTLY_TIME=23:30`）且当天未做 → `memory.nightly()`

全部有状态统计（`/api/keeper`：ticks / life_events / nightlies / errors / last_error）。

## 7. 上下文装配（一次回复）

```
system: persona 卡（当前角色）
system: 能力说明（识图/语音/表情包等）
system: 你记得关于他的事：<memories 命中>
system: 今天和他聊过的重点：<day_memo 当天备忘>
system: 你自己这一两天：<生活线>
system: 社交线（好友空间动态）
system: 你现在的情绪：<mood>
system: 最近说过的话（防复读）
user:   历史对话 N 条 + 当前消息
```

`/api/chat` 返回值里带 `context` 字段，可直接看到本次注入了什么。实测一次 1487 prompt tokens（缓存命中 1152），1.2s 左右出话。

## 8. 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/mind` | 情绪 + 生活线 + 心跳 + 记忆一步看全 |
| GET/POST | `/api/mood` | 读写情绪 |
| GET | `/api/memory` | 列出事实（`?limit=`） |
| POST | `/api/memory` | 手写一条（`{content,kind}`） |
| DELETE | `/api/memory/{id}` | 删一条事实 |
| GET | `/api/memory/events` | 事件卡（`?limit=&kind=`） |
| DELETE | `/api/memory/events/{id}` | 删事件卡 |
| GET | `/api/memory/days` | 每日摘要 |
| POST | `/api/memory/reflect` | 手动触发一次反思（测试用） |
| POST | `/api/memory/nightly` | 手动触发日结（测试用） |
| GET | `/api/life` | 今日日程 + 生活日志 |
| POST | `/api/life/tick` | 手动推进一拍（测试用） |
| GET | `/api/keeper` | 心跳状态 |
| POST | `/api/keeper/tick` | 立刻跑一拍 |

## 9. 开关与参数（`.env`）

```ini
MEMORY_RECALL_K=8
REFLECTION_ENABLED=1
MOOD_ENABLED=1
LIFE_ENABLED=1
LIFE_TICK_SECONDS=900
LIFE_EVENT_PROBABILITY=0.5
NIGHTLY_TIME=23:30
MOOD_BASE_VALENCE=0.44
```

## 10. 已知边界

- 检索是关键词 LIKE，不是向量；超出关键词的联想会漏（后续可加 sqlite-vec 或外部 embedding）
- 反思是每条消息触发，成本约为回复本身的 1/3；可改成「每 3–5 轮一次」或按显著度抽样
- 生活线是「叙述」而非「世界模型」：她不会真的记住三天前具体吃了什么，除非进了 `events` 或 `day_summary`
- 每天日程由模型生成，可能出现与现实时间不符的安排（如半夜在改稿），靠提示词约束，未做硬校验


## 隐私边界（不同好友之间）

她是一个「人」，所以情绪、生活线、空间动态是共享的；但**私密内容按 QQ 号隔离**。

| 数据 | 范围 | 说明 |
| --- | --- | --- |
| `messages` | 按 `user_id` | 最近 30 条对话只属于该好友 |
| `memories` | 按 `user_id`；`user_id IS NULL` 为「关于她自己」的通用事实 | 写入时带 uid，检索时 `user_id = uid OR user_id IS NULL` |
| `events` | 严格按 `user_id` | 情节记忆不跨人 |
| `day_notes` | 按 `(day, user_id)` 唯一 | 和每个人各一份日结，只注入给本人 |
| `day_summary` | 全局一份 | 只用生活线生成，**禁止写任何人的对话内容** |
| `life_log` / `mood` / `qzone_feeds` | 全局（按角色卡） | 她自己的生活，共享 |

- 23:30 心跳：先跑全局日结（`nightly(day)`，生活线专属 prompt），再对当天聊过的人逐个跑 `nightly(day, uid=...)`，各写一条 `day_notes`。
- 手动补跑：`POST /api/memory/nightly?uid=10001`。
- 反向检查：`/api/chat` 返回的 `context.memory_text` 里不该出现别的 QQ 号相关的事实或日结。
