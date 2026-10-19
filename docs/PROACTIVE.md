# 主动行为：戳一戳 / 自己发说说

> 对应代码：`qqbot/proactive.py`、`qqbot/autoreply.py`（分条与补话）、`qqbot/qzone.py`（空间 sidecar 客户端）

## 1. 分条回复（像真人连发）

- 一次回复拆成 **2~3 条短消息**，条与条之间 0.5~1.6 秒，再按字数追加 `len/60` 秒。
- 提示词位置很关键：多气泡规则必须放在**历史之后、用户消息之前**（放进上下文块会被忽略，实测只回 1 条）。
- 分隔符用 `|||`（JSON mode 在长中文上下文下会返回纯空白，已弃用）。
- 主回复后有 **30%** 概率在 25~90 秒补一句（若这段时间用户先说话则取消，避免显得机械）。

实测：`在吗在吗` → 2 条，`latency=1989ms`。

## 2. 主动戳一戳

- 定时器每 15 分钟看一次，命中条件：方案开启、在 9:00–23:00、今日次数 < 上限（默认 3）、距上次 ≥ 冷却（默认 3 小时）、用户最后一次发言距今 ≥ 90 分钟。
- 命中后 **50%** 概率真的戳；戳完按 `POKE_MESSAGE_PROBABILITY`（默认 0.7）让她写一句 ≤15 字的短话跟上去。
- 每次动作写 `audit` + `life_log`，并计入 `pokes_today`。

## 3. 自己发 QQ 空间说说

不是"一天一条"的定时任务，而是**她自己判断**：

| 触发 | 概率 | 说明 |
| --- | --- | --- |
| 聊天结束后 | 15% | 从当天聊天 + 情绪 + 生活线里找"值得发出去"的片段 |
| 每 15 分钟的空闲念头 | 12% | 主动"想发点什么"的冲动 |

护栏：每天 ≤ 2 条、间隔 ≥ 3 小时、只在 10:00–22:00；模型判断"今天没什么想发的"时回 `NO`，直接不发。没登录空间时跳过（不会报错刷屏）。

### 空间登录（免扫码）

不用扫码：从 NapCat 直接取 qzone 域 Cookie，写进 sidecar 的缓存即可。

```powershell
# 1. 桥接在 3001；取 cookie（需 ADMIN_TOKEN）
GET /api/napcat/cookies?domain=qzone.qq.com
# 2. 写到 .qzone/cookies.json  {"last_used": <unix秒>, "cookies": {...}}
# 3. 起 sidecar
cd services\onebot-qzone; npm.cmd run dev
```

sidecar 起来后 `http://127.0.0.1:5700/status` 可以看到 QQ 号与 Cookie 状态；`/api/qzone/status`（3001）能返回 `login.user_id`。
Cookie 由 sidecar 每 4 小时静默续期；过期了重新执行上面 1~2 步即可。

### 相关接口（桥接 3001）

- `GET /api/qzone/status` 登录状态
- `GET /api/qzone/posts` 自己的说说列表
- `POST /api/proactive/feed/preview` 只让她"想"一条，不发布
- `POST /api/proactive/feed` 真的发（她先判断要不要发）
- `POST /api/proactive/poke` 立刻戳一次（同样走条件检查）
- `GET /api/proactive` 今日次数、冷却、上次内容与错误

## 4. 刷好友的 QQ 空间

> 代码：`qqbot/social.py`，表 `qzone_feeds`（`data/qqbot.db`）

- 由 keeper 每 15 分钟带一次：落在 9:00–23:00、今日次数 < 6、距上次 ≥ 90 分钟，再按 `SOCIAL_BROWSE_PROBABILITY`（默认 0.35）决定这一拍要不要真的去刷。
- 拉好友动态（`get_friend_feeds`）→ 跳过自己发的（按 `self_id` 过滤）→ 新 tid 入库 → 写一条生活线（"刷到 X 发了…"）。
- 最近 3 条刷到的动态会进她的聊天上下文（`social_text`），所以她**聊天时可能顺口提起**朋友发了什么；每条动态只提一次，不会反复念。
- 只读不互动：不自动点赞/评论。真机要「看到好友空间」的前提是好友那边发过说说（现在好友只有主号，主号没发过就会一直是空的）。

### 社交接口

- `GET /api/social` 今日刷了几次、限额、窗口、上次时间
- `GET /api/social/feeds` 她刷到过的动态
- `POST /api/social/browse?force=true&num=8` 立刻刷一次（调试用）

配置项：`SOCIAL_ENABLED`、`SOCIAL_BROWSE_INTERVAL_SECONDS=5400`、`SOCIAL_BROWSE_DAILY_MAX=6`、`SOCIAL_BROWSE_PROBABILITY=0.35`、`SOCIAL_WINDOW_START=9`、`SOCIAL_WINDOW_END=23`、`SOCIAL_NUM=8`。


## 给好友说说点赞 / 评论

| 触发方式 | 行为 |
| --- | --- |
| 聊天里说「点个赞 / 点一下赞 / 赞一下」 | 找到对应好友（或最近一条好友说说）→ 真点赞，然后回一句「已经点完了」 |
| 聊天里说「评论一下 / 说一句 / 留个言」 | 用当前角色口吻生成 ≤20 字评论 → 真发出去 |
| 心跳刷到新说说（`SOCIAL_AUTO_LIKE_PROBABILITY`，默认 0.4） | 自己点个赞 |
| 主人（`OWNER_UID`）的说说（`SOCIAL_AUTO_COMMENT_PROBABILITY`，默认 0.12） | 自己留一句评论 |

接口：`POST /api/social/like {tid, uin}`、`POST /api/social/comment {tid, uin, content?}`（content 省略时就地生成）。
落库：`qzone_feeds.liked / comment_text / interacted_at`；动作同时写 `life_log`（她能记住「我给你点了赞」）。

注意：`send_like` 的消息段与点赞列表接口（`get_like_list`）经常返回 1401，点赞结果以说说里的 `likenum` 为准。
