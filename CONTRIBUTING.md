# 贡献指南

感谢你愿意改进这个项目。它是一个「自己用着舒服」的小项目，提交前请先看下面几条。

## 开发环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONUTF8=1
.\.venv\Scripts\python.exe run.py
```

不需要真的 QQ 也能跑通大部分逻辑：`tests/fake_onebot.py` 是一个假的 OneBot 网关。

```powershell
# 终端 A：假网关
.\.venv\Scripts\python.exe tests\fake_onebot.py
# 终端 B：桥接
.\.venv\Scripts\python.exe run.py
# 终端 C：端到端冒烟
.\.venv\Scripts\python.exe tests\e2e.py
```

## 提交前自查

- [ ] 没有把 `.env`、`data/`、`logs/`、`.qzone/`、`models/`、`napcat*/` 提交进来
- [ ] 代码里没有写死自己的 QQ 号、昵称、路径
- [ ] 新增配置项同步写进 `.env.example` 和 `docs/CONFIG.md`
- [ ] `python -c "import qqbot.main"` 能过
- [ ] 新功能有对应的接口或文档说明

## 代码风格

- Python 3.10+，行宽 100 左右，优先可读
- 所有对外接口必须带 `Depends(require_token)`
- 发消息、调模型、写库都要有超时或异常兜底；失败时记录日志而不是抛出到循环外
- 涉及 QQ 发送的地方要注意频率（`tools.Actions._guard_send`）

## 提交信息

```
feat: 简短描述
fix: 修了什么
docs: 只改文档
```

## License

提交即表示同意以 [MIT](../LICENSE) 协议发布你的贡献。
