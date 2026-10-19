# 语音识别（听语音）

QQ 语音消息发来的时候，NapCat 给的是一个 `.amr` 结尾的文件，**实际内容是 SILK v3**（`#!SILK_V3`）。
所以不能直接丢给语音模型，必须先解码。整条链路：

```
QQ 语音（SILK，文件名骗人地写成 .amr）
  └─ 剥掉开头多出来的 1 个字节（常见 0x02 / 0x03）
      └─ silk_v3_decoder.exe  →  16kHz 单声道 PCM
          └─ 包成 WAV
              └─ sherpa-onnx + SenseVoice（本地推理）
                  └─ 「你能听见我说话吗」
```

识别结果会被塞进上下文，变成 `[语音：你能听见我说话吗]`，她按正常聊天逻辑回。

## 为什么选这套

| 方案 | 结论 |
| --- | --- |
| 云端 ASR（阿里/腾讯/讯飞） | 要额外 key、按量计费、语音要出本机 |
| whisper.cpp | Windows 上要自己编，中文小模型效果一般 |
| **sherpa-onnx + SenseVoice** | 纯 CPU、离线、有现成 Python 轮子（`sherpa-onnx`，cp314 有预编译）、中文实测准、一条 2 秒语音 0.2 秒出字 |
| `pilk`（SILK 解码 Python 包） | 是 C 扩展，Windows + Python 3.14 没有轮子，装不上编译不过 |
| **`silk_v3_decoder.exe` 预编译** | kn007/silk-v3-decoder 的 release 里有 win32 版，179KB，直接能用 |

版本：`sherpa-onnx==1.13.8`、模型 `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09`（158MB 压缩 / 226MB 解开，`model.int8.onnx` + `tokens.txt`）。

## 文件放哪

```
models/sense-voice/            # 模型（.gitignore 忽略）
  ├─ model.int8.onnx
  └─ tokens.txt
tools/silk/silk_v3_decoder.exe  # SILK 解码器（.gitignore 忽略）
data/voice/                     # 解出来的 silk / wav 缓存
qqbot/asr.py                    # 封装
```

## 配置（.env）

```ini
ASR_ENABLED=1
ASR_ENGINE=sensevoice          # sensevoice | paraformer
ASR_LANGUAGE=auto              # auto / zh / en / ja / ko / yue（SenseVoice 可用）
ASR_THREADS=4
ASR_MODEL_DIR=models/sense-voice
ASR_PARAformer_DIR=models/paraformer-zh
ASR_SILK_EXE=tools/silk/silk_v3_decoder.exe
ASR_TIMEOUT=30
ASR_MAX_BYTES=10485760
VOICE_DIR=data/voice
```

## 引擎对比（真实语音样本，2026-10-19）

两条链路共用「SILK 解码 → 16k wav」，只换识别器：

| 样本（内容） | SenseVoice | Paraformer |
| --- | --- | --- |
| `54c1a37e…` 来来来告诉我为什么 | ✅ 0.12s | ✅ 0.09s |
| `1ebf1a94…` 能听见我说话吗 | ✅ 0.15s | ✅ 0.10s |
| `d42a8191…` 为什么不敢听 | ✅ 0.15s | ❌「为什么不敢**踢**我」0.09s |
| `9a971e54…` 你能听见我说话吗 | ✅ 0.13s | ✅ 0.09s |

结论：Paraformer 稍快，但会把「不敢听」听成「不敢踢我」；**继续用 SenseVoice**（情绪更稳，也带语种自动识别）。
想换引擎只改 `ASR_ENGINE=paraformer`，模型目录 `models/paraformer-zh`（`sherpa-onnx-paraformer-zh-int8-2025-10-07`，238MB）。
对比脚本：`scripts/bench_asr.py`。

## 优化（2026-10-19 追加）

- **长语音切块**：超过 `ASR_CHUNK_SECONDS`（默认 30 秒）自动切块识别再拼接，块间留 0.1 秒重叠并用最长公共前后缀去重；之前 60 秒以上的语音会解码超时、识别失败。
- **识别缓存**：按 SILK 文件 md5 缓存结果到 `data/voice/<md5>.asr.json`，同一段语音重发不再重复解码；换引擎会失效重跑。
- **主备引擎兜底**：SenseVoice 返回空文本时，自动用 `ASR_FALLBACK`（默认 paraformer）再听一遍。
- **文本清洗**：去掉中文之间多余的空格、合并重复标点。
- **情绪旁白**：SenseVoice 的 `emotion` token（`<|HAPPY|>` 等）会转成一句旁白附在语音文本后，例如
  `[语音：你能听见我说话吗]（他这条语音的语气有点低落）`，大脑能顺势接情绪；由 `ASR_EMOTION_HINT` 控制。

```ini
ASR_CHUNK_SECONDS=30
ASR_FALLBACK=paraformer
ASR_CACHE=1
ASR_EMOTION_HINT=1
```

实测：8 秒拼接语音切成 5 块识别共 2.5 秒；重复识别命中缓存（`cached` 计数 +1）；真实两条语音仍是「你能听见我说话吗」「为什么不敢听」，`emotion=<|NEUTRAL|>`、`lang=<|yue|>`。

## 参考项目（GitHub 调研）

| 项目 | 星 | 为什么看它 |
| --- | --- | --- |
| `modelscope/FunASR` | 20.7k | SenseVoice / Paraformer 的官方仓库，模型卡与参数说明 |
| `k2-fsa/sherpa-onnx` | 15.4k | 纯 C++/ONNX 推理，带 Python 轮子，离线跨平台 |
| `FunAudioLLM/SenseVoice` | 9.5k | 多语种（中英日韩粤）+ 情绪/事件 token |
| `ggerganov/whisper.cpp` | 54.5k | 备选；Windows 要自己编，中文小模型一般 |
| `kn007/silk-v3-decoder` | 3.2k | 解码 QQ 的 SILK，release 里带 Windows 可执行文件 |


## 接口

| 接口 | 说明 |
| --- | --- |
| `GET /api/asr` | 状态（enabled / loaded / 统计 / 模型路径） |
| `POST /api/asr/transcribe` | body `{"file": "..."}` 或 `{"url": "..."}`，返回 `{"ok", "text"}` |
| `GET /api/state` | 里也有 `asr` 块 |

## 实测

| 用例 | 结果 | 耗时 |
| --- | --- | --- |
| 九的语音 1（4075B silk） | 「你能听见我说话吗」 | 2.63s（含首次加载模型） |
| 九的语音 2（4130B silk） | 「为什么不敢听」 | 0.18s |
| 同一条走 HTTP `POST /api/asr/transcribe` | 「为什么不敢听」 | 2.3s（含加载），之后 0.2s 左右 |

## 已知边界

- 语音消息里的本地 `path` 指向腾讯文件目录，NapCat 会保留原始 silk；路径不存在时用 `url` 兜底下载（rkey 会过期）。
- 群里 @ 我的语音同样能识，但只对「好友私聊 + 群聊被叫到」的既有回复规则负责。
- SenseVoice 自带情绪/事件 token（笑声、愤怒等），目前只取文字，情绪 token 没接进 mood 系统。
- 超过 60 秒的语音目前解码超时会失败（返回空文本，聊天里显示 `[语音]`）。
