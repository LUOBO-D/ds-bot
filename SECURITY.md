# 安全策略

## 报告问题

发现安全问题请**不要**开公开 issue，直接给维护者发私信或邮件（仓库主页有联系方式）。

## 这个项目会接触到哪些凭据

| 凭据 | 存放位置 | 泄露后果 |
| --- | --- | --- |
| 大模型 API Key | `.env` 的 `DEEPSEEK_API_KEY` | 被盗刷 |
| 管理 Token | `.env` 的 `ADMIN_TOKEN` | 可调用本机所有接口（发消息、读图、读记忆） |
| QQ 空间 Cookie | `.qzone/cookies.json` | **等价于 QQ 登录态**，可直接操作空间 |
| QQ 登录态 | 由 NapCat 管理（`napcat*/` 目录） | 可发消息、看好友 |

以上路径都已在 `.gitignore` 里。提交前请确认：

```powershell
git status --short
git check-ignore -v .env data/qqbot.db .qzone/cookies.json
```

## 部署建议

1. 桥接只监听 `127.0.0.1`，不要直接暴露公网。
2. 需要远程访问时，用 WireGuard / Tailscale / SSH 隧道，或在反代上加 HTTPS + 强鉴权。
3. 不要把 `.env`、`data/`、`napcat*/` 打包发给别人。
4. 定期轮换 `ADMIN_TOKEN` 与大模型 Key；空间 Cookie 过期后重新注入。

## 已知风险

- 个人 QQ 号接第三方协议端（NapCat）属于非官方使用方式，存在被风控、限制、封号的风险。
- QQ 空间相关接口来自逆向，随时可能失效。
- 本项目对「模型被提示词注入后做了危险动作」没有沙箱保护：它只会在你给它的权限内发消息。
  如果接入不可信群聊，建议关闭自动回复或限制在白名单。
