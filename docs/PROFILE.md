# 她自己的个人主页（头像 / 个性签名）

> 2026-10-20 上线。让她像真人一样维护自己的 QQ 资料，而不是一直停在出厂状态。

## 能改什么

| 项目 | 接口 | 说明 |
| --- | --- | --- |
| 个性签名 | NapCat `set_self_longnick` | 她结合人设、当天生活线、当前心情自己写，≤24 字 |
| 头像 | 无权限 | 2026-10-20 按要求收回，她只能看、不能改 |
| 昵称 | 不改 | 保持「大肥鱼」 |

## 自动维护

- 心跳（`qqbot/keeper.py`，15 分钟一拍）里调 `ProfileService.tick()`
- 签名：距上次 ≥ `PROFILE_SIGNATURE_HOURS`（默认 20 小时）才换一次；内容为空或和上次一样就跳过
- 网关没连上时直接跳过，不会写错
- 所有动作进 `audit` 表（`profile.signature` / `profile.avatar`）

## 手动接口（Bearer `ADMIN_TOKEN`）

```http
GET  /api/profile                          # 昵称、当前签名、头像路径、统计
POST /api/profile/refresh?force=true       # 让她现在写一条新签名并设上去
POST /api/profile/signature  {"text":"…"}  # 直接指定签名
# 头像已收回权限，没有写接口
```

## 泡制方式

`qqbot/profile.py::SIGNATURE_PROMPT` 里给了「人设节选 + 今天的生活 + 现在的心情 + 上一次签名」，要求一句话、不堆 emoji、不写自我介绍、不和上次雷同。

实测输出示例：

- 「蹲久了腿麻，坐地板听芝麻嚼猫粮咔咔响。」
- 「校样摊了一上午，纸边翻得有点毛。」

## 开关

`.env`：

```
PROFILE_ENABLED=1
PROFILE_SIGNATURE_HOURS=20
PROFILE_SIGNATURE_MAX=24
```

## 已知边界

- 头像由人手动设置，她只有读权限
- 签名只在心跳时更新，不会在聊天中途突然改（避免打断对话）
- QQ 空间主页的「空间签名 / 空间头像」不在 NapCat 能力里，暂未接
