# ds-bot 一键安装脚本（Windows）
# 用法：双击 install.bat，或在 PowerShell 里运行
#   powershell -ExecutionPolicy Bypass -File scripts\install.ps1
# 参数：
#   -SkipNapCat  不下载 NapCat 安装器（已有网关时可以跳过）
#   -WithAsr     额外下载语音识别模型（约 170MB）

param(
    [switch]$SkipNapCat,
    [switch]$WithAsr
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Say ""; Say "==> $msg" "Cyan" }

Say "============================================" "Cyan"
Say "  ds-bot 一键安装" "Cyan"
Say "============================================" "Cyan"
Say "项目目录：$root"

# ---------------------------------------------------------------- 1. Python
Step "检查 Python（需要 3.10 以上）"
$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $ver = & py -3 -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver) { $python = @("py", "-3") }
}
if (-not $python -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver) { $python = @("python") }
}
if (-not $python) {
    Say "[x] 没有找到 Python。请先去 https://www.python.org/downloads/ 安装 3.10+" "Red"
    Say "    安装时记得勾选 Add python.exe to PATH，装好后重新运行 install.bat" "Yellow"
    Start-Process "https://www.python.org/downloads/"
    exit 1
}
Say "[+] Python $ver" "Green"

# ---------------------------------------------------------------- 2. 虚拟环境
Step "创建虚拟环境 .venv"
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $python[0] @($python[1..($python.Count - 1)]) -m venv .venv
    if ($LASTEXITCODE -ne 0) { Say "[x] 创建虚拟环境失败" "Red"; exit 1 }
    Say "[+] 已创建 .venv" "Green"
}
else {
    Say "[=] .venv 已存在，跳过" "DarkGray"
}

# ---------------------------------------------------------------- 3. 依赖
Step "安装 Python 依赖（约 40MB，第一次会慢一点）"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Say "[!] 默认源失败，改用清华镜像重试" "Yellow"
    & $venvPy -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
}
if ($LASTEXITCODE -ne 0) { Say "[x] 依赖安装失败，请检查网络后重试" "Red"; exit 1 }
Say "[+] 依赖安装完成" "Green"

# ---------------------------------------------------------------- 4. 配置
Step "生成 .env 配置"
& $venvPy (Join-Path $root "scripts\setup_env.py")
if ($LASTEXITCODE -ne 0) { Say "[x] 生成 .env 失败" "Red"; exit 1 }

# ---------------------------------------------------------------- 5. NapCat
if (-not $SkipNapCat) {
    Step "下载 NapCat 一键安装器（约 1MB）"
    $cache = Join-Path $root ".cache"
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $zip = Join-Path $cache "NapCat.OneKey.zip"
    $target = "https://github.com/NapNeko/NapCatQQ/releases/latest/download/NapCat.Shell.Windows.OneKey.zip"
    $mirrors = @(
        "https://ghfast.top/$target",
        "https://ghproxy.net/$target",
        "https://gh-proxy.com/$target",
        $target
    )
    if (-not (Test-Path $zip)) {
        $ok = $false
        foreach ($url in $mirrors) {
            try {
                Say "    下载中：$url" "DarkGray"
                Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 120
                $ok = $true
                break
            }
            catch {
                Say "    失败，换下一个镜像" "DarkYellow"
            }
        }
        if (-not $ok) {
            Say "[!] NapCat 下载失败。可以手动下载：" "Yellow"
            Say "    $target" "Yellow"
            Say "    解压后把 NapCatInstaller.exe 放到项目的 napcat-onekey 目录" "Yellow"
        }
    }
    if (Test-Path $zip) {
        $out = Join-Path $root "napcat-onekey"
        if (-not (Test-Path $out)) {
            Expand-Archive -Path $zip -DestinationPath $out -Force
        }
        Say "[+] NapCat 安装器已解压到 napcat-onekey\" "Green"
        Say "    下一步：双击 napcat-onekey\NapCatInstaller.exe" "Yellow"
        Say "    它会自动下载 QQ 并装好 NapCat，完成后再运行 start.bat" "Yellow"
    }
}

# ---------------------------------------------------------------- 6. 语音模型（可选）
if ($WithAsr) {
    Step "下载语音识别模型（约 170MB）"
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "scripts\fetch_asr.ps1")
}

Say ""
Say "============================================" "Cyan"
Say "  安装完成！接下来：" "Cyan"
Say "============================================" "Cyan"
Say "  1. 用记事本打开 .env，填好 DEEPSEEK_API_KEY（必须）" "White"
Say "  2. 双击 napcat-onekey\NapCatInstaller.exe 安装网关（首次）" "White"
Say "  3. 双击 start.bat 启动" "White"
Say "  4. 扫码登录后，在 NapCat WebUI（http://127.0.0.1:6099）把反向 WS 指到 ws://127.0.0.1:3001/onebot/v11/ws" "White"
Say ""
