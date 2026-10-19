# ds-bot 语音识别模型下载（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File scripts\fetch_asr.ps1
# 内容：SILK 解码器（silk_v3_decoder.exe）+ SenseVoice int8 模型（约 170MB）

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

$tools = Join-Path $root "tools\silk"
$models = Join-Path $root "models\sense-voice"
New-Item -ItemType Directory -Force -Path $tools, $models | Out-Null

Say "==> 下载 SILK 解码器" "Cyan"
$decoder = Join-Path $tools "silk_v3_decoder.exe"
if (Test-Path $decoder) {
    Say "[=] 已存在，跳过" "DarkGray"
}
else {
    $urls = @(
        "https://ghfast.top/https://github.com/kn007/silk-v3-decoder/releases/download/1.0/silk_v3_decoder.exe",
        "https://ghproxy.net/https://github.com/kn007/silk-v3-decoder/releases/download/1.0/silk_v3_decoder.exe",
        "https://github.com/kn007/silk-v3-decoder/releases/download/1.0/silk_v3_decoder.exe"
    )
    $ok = $false
    foreach ($url in $urls) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $decoder -UseBasicParsing -TimeoutSec 90
            $ok = $true; break
        }
        catch { Say "    换镜像重试" "DarkYellow" }
    }
    if (-not $ok) { Say "[!] 解码器下载失败，语音功能会不可用（其余功能不受影响）" "Yellow" }
    else { Say "[+] silk_v3_decoder.exe 就绪" "Green" }
}

Say "==> 下载 SenseVoice 模型（int8，约 160MB）" "Cyan"
$model = Join-Path $models "model.int8.onnx"
$tokens = Join-Path $models "tokens.txt"
if ((Test-Path $model) -and (Test-Path $tokens)) {
    Say "[=] 模型已存在，跳过" "DarkGray"
}
else {
    $archive = Join-Path $root ".cache\sense-voice.tar.bz2"
    New-Item -ItemType Directory -Force -Path (Split-Path $archive) | Out-Null
    $urls = @(
        "https://hf-mirror.com/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/sense-voice.tar.bz2",
        "https://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17/resolve/main/sense-voice.tar.bz2"
    )
    $ok = $false
    foreach ($url in $urls) {
        try {
            Say "    下载中：$url" "DarkGray"
            Invoke-WebRequest -Uri $url -OutFile $archive -UseBasicParsing -TimeoutSec 1800
            $ok = $true; break
        }
        catch { Say "    失败，换下一个源" "DarkYellow" }
    }
    if ($ok) {
        Say "    解压中..." "DarkGray"
        tar -xjf $archive -C $models --strip-components=1
        if (Test-Path $model) { Say "[+] 模型就绪" "Green" }
        else { Say "[!] 解压后没找到 model.int8.onnx，请手动解压到 models\sense-voice" "Yellow" }
    }
    else {
        Say "[!] 模型下载失败，语音功能会不可用（其余功能不受影响）" "Yellow"
    }
}

Say "==> 在 .env 里设置 ASR_ENABLED=1 后重启 start.bat 即可" "Cyan"
