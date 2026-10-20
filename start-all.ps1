# 一键启动：NapCat 网关 → QQ 空间 sidecar → 桥接服务
#
# 用法：
#   .\start-all.ps1                        # 全部启动（已在跑的自动跳过）
#   .\start-all.ps1 -Account 3945832544    # 指定要登录的 QQ
#   .\start-all.ps1 -CheckOnly             # 只看端口状态，不启动
#   .\start-all.ps1 -SkipNapCat -SkipSidecar
#
[CmdletBinding()]
param(
    [string]$Account = "",
    [switch]$SkipNapCat,
    [switch]$SkipSidecar,
    [switch]$SkipBridge,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync("127.0.0.1", $Port)
        return ($task.Wait(600) -and $client.Connected)
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Wait-Port([int]$Port, [int]$Seconds, [string]$Label) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (Test-Port $Port) {
            Write-Host "      [ok] $Label 就绪（端口 $Port）" -ForegroundColor Green
            return $true
        }
        Start-Sleep -Milliseconds 1000
    }
    Write-Host "      [!!] $Label 没在 $Seconds 秒内起来（端口 $Port）" -ForegroundColor Yellow
    return $false
}

Write-Host ""
Write-Host "===== qqbot 一键启动 =====" -ForegroundColor Cyan

$bridgeUp = Test-Port 3001
$webuiUp = Test-Port 6099
$sidecarUp = Test-Port 5700
Write-Host ("  当前端口：NapCat(6099)={0}  QQ空间(5700)={1}  桥接(3001)={2}" -f $webuiUp, $sidecarUp, $bridgeUp)

if ($CheckOnly) {
    if ($bridgeUp) {
        Write-Host "  桥接正在运行：http://127.0.0.1:3001" -ForegroundColor Green
    }
    Write-Host "  检查完成（-CheckOnly，没有启动任何东西）" -ForegroundColor Cyan
    exit 0
}

if ($bridgeUp) {
    Write-Host "  桥接已经在 3001 上跑，先关掉旧的再启动（避免端口占用）。" -ForegroundColor Yellow
    Write-Host "  可以运行：.\stop-all.ps1" -ForegroundColor Yellow
    exit 1
}

# ---------------------------------------------------------------- 1. NapCat
if ($SkipNapCat) {
    Write-Host "[1/3] 跳过 NapCat（-SkipNapCat）"
} elseif ($webuiUp) {
    Write-Host "[1/3] NapCat 已经在运行（6099），跳过" -ForegroundColor DarkGray
} else {
    $launcher = Join-Path $PSScriptRoot "napcat-qq\start-napcat.bat"
    if (-not (Test-Path -LiteralPath $launcher)) {
        Write-Host "[1/3] 找不到 $launcher" -ForegroundColor Red
        Write-Host "      请先看 docs/SETUP.md 里的 NapCat 安装步骤。" -ForegroundColor Red
        exit 1
    }
    Write-Host "[1/3] 启动 NapCat 网关（账号 $Account，第一次可能需要手机扫码）..."
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "`"$launcher`" -q $Account" `
        -WorkingDirectory (Split-Path -LiteralPath $launcher) `
        -WindowStyle Minimized
    if (-not (Wait-Port 6099 120 "NapCat WebUI")) {
        Write-Host "      NapCat 没起来：看 napcat-qq\...\napcat\logs 里的日志，或先手动跑 start-napcat.bat 看报错。" -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------- 2. sidecar
$qzoneDir = Join-Path $PSScriptRoot "services\onebot-qzone"
if ($SkipSidecar) {
    Write-Host "[2/3] 跳过 QQ 空间 sidecar（-SkipSidecar）"
} elseif ($sidecarUp) {
    Write-Host "[2/3] QQ 空间 sidecar 已经在运行（5700），跳过" -ForegroundColor DarkGray
} elseif (-not (Test-Path -LiteralPath $qzoneDir)) {
    Write-Host "[2/3] 没有 services\onebot-qzone，跳过空间功能（聊天不受影响）" -ForegroundColor DarkGray
} else {
    Write-Host "[2/3] 启动 QQ 空间 sidecar..."
    Start-Process -FilePath "cmd.exe" `
        -ArgumentList "/c", "npm.cmd run dev" `
        -WorkingDirectory $qzoneDir `
        -WindowStyle Minimized
    if (-not (Wait-Port 5700 90 "QQ 空间 sidecar")) {
        Write-Host "      sidecar 没起来：进 services\onebot-qzone 手动跑 npm run dev 看报错。" -ForegroundColor Yellow
        Write-Host "      它的 Cookie 过期时，用：python scripts\refresh_qzone_cookie.py" -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------- 3. 桥接
if ($SkipBridge) {
    Write-Host "[3/3] 跳过桥接（-SkipBridge）"
    Write-Host ""
    Write-Host "  其它服务已经拉起来了。要单独起桥接：.\start.ps1" -ForegroundColor Cyan
    exit 0
}

$token = ""
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    $line = Get-Content -LiteralPath $envFile | Where-Object { $_ -match '^ADMIN_TOKEN=' } | Select-Object -First 1
    if ($line) { $token = $line.Substring("ADMIN_TOKEN=".Length).Trim() }
}

Write-Host "[3/3] 启动桥接（前台运行，Ctrl+C 退出）..."
Write-Host ""
Write-Host "  管理接口 : http://127.0.0.1:3001" -ForegroundColor Cyan
if ($token) { Write-Host "  管理 token: $token" -ForegroundColor Cyan }
Write-Host "  WebUI    : http://127.0.0.1:6099/webui" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  QQ 里给「大肥鱼」发条消息，看日志有没有『自动回复』就知道通没通。" -ForegroundColor DarkGray
Write-Host ""

& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\run.py"
