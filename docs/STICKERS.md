# 表情包库

她自己的表情包收藏：好友发来的表情包，她**自己判断喜不喜欢**，喜欢的才收进库；以后聊天时偶尔挑一张发出去，像真人斗图。

## 数据流

```
好友发表情包
   └→ 识图（qqbot/vision.py，deepseek-chat + OCR）
        └→ 判断（qqbot/stickers.py judge：喜欢 / 不喜欢，2~4 个标签 + 一句自用备注）
             ├→ 喜欢 → stickers 表（打分 0.6~1.0，容量上限 STICKER_MAX=120，超了删最旧的）
             └→ 不喜欢 → 丢弃（只留 images 表里的识别缓存）
她回复完文字
   └→ 30% 概率随手挑一张（按评分加权）以「自定义表情包」发出
        └→ 约束：15 分钟冷却、每天最多 12 张、库里已有同一张不会连发
```

## 表结构（`stickers`）

| 字段 | 说明 |
| --- | --- |
| `id` | 自增 |
| `at` | 入库时间（unix） |
| `digest` | 图片 md5，用于去重（同一张表情包只收一次） |
| `path` | 本地文件路径（`data/inbox/`） |
| `summary` | 识图描述（【文字】/【表情】） |
| `tags` | 她给的标签，逗号分隔，如 `可爱,傲娇,搞笑` |
| `note` | 她的自用备注，如「下次互怼时发这个」 |
| `rating` | 喜欢程度 0.6~1.0，决定被挑中的概率 |
| `used_count` / `last_used_at` | 发过几次 / 上次发的时间 |
| `persona` | 收这张卡时生效的角色，便于多角色隔离 |

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/stickerbook` | 库内容 + 运行状态（判断数、收藏数、发出数） |
| POST | `/api/stickerbook/judge` | 手动喂一张（`file` 本地路径或 `url`），让她现场判断收不收 |
| POST | `/api/stickerbook/send` | 指定 `sticker_id` 发某一张；不指定则按概率随手挑一张（`force` 语义） |
| DELETE | `/api/stickerbook/{id}` | 删掉某张 |
| GET | `/api/mind` | 其中 `stickers` 字段是同样状态 |

原有 `/api/stickers` 是**内置**的 QQ 表情（face）清单，和这里的自定义表情包不是一回事。

## 配置（`.env`）

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `STICKER_ENABLED` | 1 | 总开关 |
| `STICKER_MAX` | 120 | 库容量，超出删最旧的 |
| `STICKER_JUDGE_TEMPERATURE` | 0.6 | 判断收不收时的随机度 |
| `STICKER_SEND_PROBABILITY` | 0.3 | 每次回复后随手发一张的概率 |
| `STICKER_SEND_DAILY_MAX` | 12 | 每天最多发几张 |
| `STICKER_SEND_COOLDOWN` | 900 | 两次发表情包之间的最短间隔（秒） |

## 已知边界

- 收到的表情包来自 QQ 的 `image` 段 `sub_type=1`；商城表情是 `mface`，也会走同一路径落库，但那条 rkey 链接过期要尽快下载（识图时已经下载过）。
- 识别错误时会判断错，可手动 `DELETE` 清掉；误收不影响她正常聊天。
- 图片本体和 `images` 表共用 `data/inbox/`，去重按 md5，不会重复占空间。
- 发表情包同样受全局发送节流（`QB_SEND_SPACING`）约束，多人同时聊不会刷屏。
- 她不会为了发表情包而发表情包：库空、冷却中、当天超限都直接跳过。
