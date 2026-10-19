# 识图（图片 / 表情包）

## 为什么以前识别不准

以 15:0x 和「九」聊天里的那张图为例（截图：中新视频「就业形势总体稳定／全国企业周平均工作时间 48.3 小时」，官员「毛盛勇」低头看稿）。

- 旧提示词：`DESCRIBE_PROMPT` 要求「一句话、25 字以内、说清有没有文字」，`max_tokens=120`。
- 结果：模型只输出「官员低头看稿，神情严肃，配就业数据文字。」——数字、单位、人名全被长度上限挤掉。
- 下游：她只拿到这句话，就被问「这数对劲吗」，只能猜（聊天里出现「末位还是 0 和 5」「我是不是又说错啦」）。

结论：不是模型不会看，是**提示词把关键信息砍了**，再加上「图里有什么字」这套活交给生成式模型本身就不可靠。

## 现在的方案（v3）

1. **文字走 OCR**：`Actions.ocr()` → NapCat 扩展 action `ocr_image`（QQ 自带的 Windows 截图 OCR），按坐标排序拼成阅读顺序，`48.3`、`毛盛勇`、`前三季度` 这类都能逐字拿到。
2. **画面走视觉模型**：`deepseek-chat`（`input_modalities: text+image`）只看图说一句「主体 + 情绪」，不再让它抄字。
3. **合并**：`【文字】<OCR 结果>` + `【画面】<模型一句>`，作为 `[图片：…]` 注入对话。
4. **表情包**：跳过 OCR，走 `STICKER_PROMPT` → `【文字】图内文字` + `【表情】一句情绪`。
5. **缓存版本**：`images.summary_version`，常量 `vision.PROMPT_VERSION=3`；旧摘要版本低于当前值会重新识别，不会一直吃旧结果。

## 接口

- `POST /api/vision/ocr` `{"file": "..."}` → `{"text": "...", "result": [...]}`（原始文本框 + 拼好的文本）
- `POST /api/vision/describe` `{"file"|"url", "sticker": bool}` → `{"summary": "【文字】…\n【画面】…"}`
- `GET /api/images` 看归档与缓存。

## 配置（.env）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `VISION_ENABLED` | 1 | 总开关 |
| `VISION_OCR_ENABLED` | 1 | 用 NapCat OCR 抽文字 |
| `VISION_MODEL` | deepseek-chat | 视觉模型（deepseek-flash 是推理模型，短输出会被 reasoning 占满，content 为空） |
| `VISION_TIMEOUT` | 30 | 单次调用超时 |
| `VISION_MAX_BYTES` | 8MB | 超过不识别 |

## 参考过的项目 / 文档

- NapCat Extended Actions：`ocr_image`（`wantWinScreenOCR`）、`fetch_custom_face`、`get_image`。
- AstrBot 生态的视觉插件走「VLM provider + 提示词模板」；搜图类插件（`soutushenqi`）先用 VLM 做视觉语义筛选。
- NoneBot2 社区常见的多模态聊天插件：图片先落盘/转 base64，再交给视觉模型，文字类截图另配 OCR。

## 已知边界

- 这是「描述」不是「看懂」：图表、艺术字、密集小字仍可能错，OCR 也会把「中新视频」认成「中新视费」。
- 表情包的「文字」靠模型转录，长句会出错；需要精确文字时走 OCR。
- 一次图片会多一次视觉调用（约 0.8–1.5s）+ 一次 OCR（约 0.2–0.6s），回复会慢一点。
