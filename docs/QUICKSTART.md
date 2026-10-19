# 小白教程：10 分钟让她在你的 QQ 里说话

> 这份文档只做一件事：**先让你用上**。
> 不讲架构、不讲配置项，照着做就行。等你能聊上了，再看 [SETUP.md](SETUP.md)（完整部署）和 [CONFIG.md](CONFIG.md)（全部参数）。

## 你需要准备

| 东西 | 说明 |
| --- | --- |
| 一台 Windows 10/11 电脑 | 别用手机，别用服务器 |
| 一个 **QQ 小号** | 专门给机器人用，不要用你主号 |
| 一个 DeepSeek API Key | 去 <https://platform.deepseek.com> 注册 → 左侧「API keys」→ 创建 |
| 10 分钟 | 第一次装 QQ 会久一点 |

---

## 第 1 步：把项目下载下来

打开 <https://github.com/LUOBO-D/ds-bot>，点绿色的 **Code → Download ZIP**，
下载完解压到一个**没有中文和空格的路径**，例如：

```
D:\ds-bot
```

> 有 Git 的话也可以：`git clone https://github.com/LUOBO-D/ds-bot`

---

## 第 2 步：双击 `install.bat`

进到 `D:\ds-bot`，找到 **install.bat**，双击它。

它会自动做这些事：

1. 检查你有没有装 Python（没有的话会打开下载页，装的时候**记得勾选 Add python.exe to PATH**）
2. 建一个 `.venv` 环境
3. 装依赖（40MB 左右，耐心等）
4. 生成 `.env` 配置文件（里面的 token 自动随机生成，不用管）
5. 下载 NapCat 安装器

看到最后有 **「安装完成！接下来：」** 就成功了。

---

## 第 3 步：填好 API Key

在 `D:\ds-bot` 里找到 **.env**，右键 → **打开方式 → 记事本**。

找到这两行，改成你自己的：

```ini
DEEPSEEK_API_KEY=sk-你的key填这里
AUTO_REPLY_ALLOWLIST=你的QQ号
```

- `DEEPSEEK_API_KEY`：就是第 0 步申请的那个
- `AUTO_REPLY_ALLOWLIST`：你自己的 QQ 号（用大号跟机器人聊的那个号）
- 想让她回复所有人，就写 `AUTO_REPLY_ALLOWLIST=*`（不建议，容易被风控）

保存，关掉记事本。

---

## 第 4 步：装 QQ 网关（NapCat）

回到 `D:\ds-bot`，进 **napcat-onekey** 文件夹，双击 **NapCatInstaller.exe**。

安装器会问你装到哪、要不要下载 QQ，一路点 **下一步 / 确定**：

- 它会自动下 QQ（300MB 左右）
- 装完后会生成一个文件夹，名字类似 `NapCat.52230.Shell` 或 `napcat-qq`

把**这个生成的文件夹整个剪切，粘贴到 `D:\ds-bot` 里面**（和 install.bat 同级）。

> 为什么？这样 `start.bat` 以后就能自动找到它。

---

## 第 5 步：双击 `start.bat` 启动

回到 `D:\ds-bot`，双击 **start.bat**。

你会看到：

1. 弹出一个 QQ 登录窗口 → **用手机 QQ 扫码登录你的小号**
2. 黑色窗口里滚动日志
3. 出现这一行就说明成功了：

```
gateway connected (self_id=你的小号QQ号)
```

> 登录过的号可以直接双击，或在命令行里用 `start.bat -Account 你的小号QQ号` 快速登录。

---

## 第 6 步：让 QQ 把消息转给桥接

打开浏览器，访问 **<http://127.0.0.1:6099/webui>**（这是 NapCat 的控制台）。

1. 第一次进可能要输入 token，token 在弹出来的黑色窗口里面有，搜 `WebUi Token`
2. 左侧点 **网络配置**（或 Network）
3. 找到 **WebSocket 客户端**，点「添加」
4. 按这样填：

| 字段 | 填什么 |
| --- | --- |
| 名称 | 随便，比如 `local` |
| URL | `ws://127.0.0.1:3001/onebot/v11/ws` |
| 消息格式 | `array` |
| Token | 留空 |
| 启用 | ✅ |

保存。黑色窗口里如果出现 `gateway connected`，就成了。

---

## 第 7 步：聊一句试试

用你的大号，私聊你的**小号**，发一句：

```
在干嘛呀
```

3 秒内应该会收到 2~3 条短消息，比如：

```
在改稿子。
第三遍了，还没改好。
你呢，忙完了吗？
```

**到这一步，就算装好了。** 剩下的都可以慢慢来。

---

## 出问题了看这里

| 现象 | 大概率原因 | 怎么办 |
| --- | --- | --- |
| 双击 install.bat 闪一下就没了 | 没装 Python | 装 Python 3.10+，安装时勾选 Add to PATH |
| 依赖装到一半报错 | 网络问题 | 重新双击 install.bat，它会自动换清华镜像 |
| QQ 登录窗口没弹出来 | NapCat 没装在项目目录里 | 把生成的文件夹放进 `D:\ds-bot`，再点 start.bat |
| 6099 打不开 | 网关没启动 / 端口被占 | 关掉所有 QQ，重新双击 start.bat |
| 黑窗口一直 `gateway disconnected` | 反向 WS 没加对 | 重做第 6 步，URL 别写错端口 |
| 发了消息她不理 | `AUTO_REPLY_ALLOWLIST` 没写你的号 | 改 .env 加上你的 QQ 号，重启 start.bat |
| 回复报错 401 | API Key 不对 | 重新复制 key，注意别带空格 |
| 二维码过期 | 超过 2 分钟没扫 | 关掉 QQ 窗口，重新 start.bat |
| 想换角色 | — | 在 QQ 里发「角色列表」或「切换角色 小鲸鱼」 |

**还不行？** 把黑色窗口最后 20 行发到 issue 里，通常一眼能看出来。

---

## 想继续折腾时再看这些

| 想做 | 看哪个文档 |
| --- | --- |
| 完整部署、手动装、排错 | [SETUP.md](SETUP.md) |
| 所有配置项什么意思 | [CONFIG.md](CONFIG.md) |
| 换模型（Claude、Gemini、本地 Ollama） | README 的「接入你自己的 API」 |
| 改角色卡、加新角色 | [PERSONAS.md](PERSONAS.md) |
| 记忆和情绪怎么工作 | [MIND.md](MIND.md) |
| 语音识别 | [ASR.md](ASR.md)、`scripts\fetch_asr.ps1` |
| QQ 空间自动发说说 | [PROACTIVE.md](PROACTIVE.md) |
| 表情包库 | [STICKERS.md](STICKERS.md) |

---

> ⚠️ 最后提醒：个人 QQ 号接第三方协议端有风控风险，请务必用**专用小号**，别拿主号折腾。
