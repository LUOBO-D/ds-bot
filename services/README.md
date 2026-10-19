# services/

这个目录放**可选的外部服务**，不随仓库分发（见根目录 `.gitignore`）。

目前只用到 [onebot-qzone](https://github.com/Gu-Heping/onebot-qzone)（QQ 空间 sidecar）：

```powershell
git clone https://github.com/Gu-Heping/onebot-qzone.git services/onebot-qzone
cd services/onebot-qzone
$env:PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
npm install
```

然后按 [../docs/SETUP.md](../docs/SETUP.md) 第 6.1 节配置 `.env` 与 Cookie。

主项目通过 `QZONE_BASE` / `QZONE_ACCESS_TOKEN` 连它，没有装也能正常运行（空间相关接口会返回未登录）。
