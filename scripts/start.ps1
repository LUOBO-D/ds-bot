# ds-bot 一键启动（Windows）
# 用法：双击 start.bat，或
#   powershell -ExecutionPolicy Bypass -File scripts\start.ps1 -Account 你的机器人QQ号
# 参数：
#   -Account   机器人 QQ 号（有的话走快速登录，不用扫码）
#   -NoNapCat  只启动桥接服务，不管网关

param(
    [string]$Account = "",
    [switch]$NoNapCat
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $root
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

function Test-Port([int]$port) {
    try {
        $client = New-Object Net.Sockets.TcpClient
        $task = $client.ConnectAsync("127.0.0.1", $port)
        if ($task.Wait(800) -and $client.Connected) { $client.Close(); return $true }
        $client.Close(); return $false
    }
    catch { return $false }
}

Say "============================================" "Cyan"
Say "  ds-bot 启动" "Cyan"
Say "============================================" "Cyan"

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Say "[x] 还没安装依赖，请先双击 install.bat" "Red"
    exit 1
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Say "[x] 缺少 .env，请先双击 install.bat" "Red"
    exit 1
}

# ---------------------------------------------------------------- 1. 网关
if (-not $NoNapCat) {
    if (Test-Port 6099) {
        Say "[=] NapCat 网关已在运行（6099 端口），跳过启动" "DarkGray"
    }
    else {
        # 优先用项目自带布局，其次 NapCat 官方布局；不要误选 .cache 里的模板
        $preferred = @(
            (Join-Path $root "napcat-qq\start-napcat.bat"),
            (Join-Path $root "napcat\launcher-user.bat"),
            (Join-Path $root "napcat\launcher.bat")
        )
        $launcher = $null
        foreach ($candidate in $preferred) {
            if (Test-Path $candidate) { $launcher = Get-Item $candidate; break }
        }
        if (-not $launcher) {
            $names = @("start-napcat.bat", "launcher-user.bat", "launcher.bat")
            foreach ($file in (Get-ChildItem -Path $root -Recurse -Depth 4 -File -ErrorAction SilentlyContinue)) {
                if ($names -notcontains $file.Name) { continue }
                if ($file.FullName -like "*\.cache\*" -or $file.FullName -like "*\node_modules\*" -or $file.FullName -like "*\.venv\*") { continue }
                $launcher = $file
                break
            }
        }
        if ($launcher) {
            Say "[+] 启动网关：$($launcher.FullName)" "Green"
            $argLine = if ($Account) { "-q $Account" } else { "" }
            Start-Process -FilePath "cmd.exe" `
                -ArgumentList "/c `"`"$($launcher.FullName)`" $argLine`"" `
                -WorkingDirectory $launcher.DirectoryName | Out-Null
            $deadline = (Get-Date).AddSeconds(90)
            while ((Get-Date) -lt $deadline -and -not (Test-Port 6099)) {
                Start-Sleep -Seconds 2
            }
            if (Test-Port 6099) {
                Say "[+] NapCat WebUI：http://127.0.0.1:6099/webui" "Green"
            }
            else {
                Say "[!] 90 秒内没等到 6099 端口，网关可能还在登录。" "Yellow"
                Say "    看一眼弹出的 QQ 窗口，扫码/快速登录后桥接会自动连上。" "Yellow"
            }
        }
        else {
            Say "[!] 没找到 NapCat 启动脚本（start-napcat.bat / launcher.bat）" "Yellow"
            Say "    如果你已经有别的网关，忽略即可；否则先运行 install.bat" "Yellow"
        }
    }
}

# ---------------------------------------------------------------- 2. 桥接
Say ""
Say "[+] 启动桥接服务：http://127.0.0.1:3001" "Green"
$tokenLine = Get-Content (Join-Path $root ".env") -Encoding utf8 | Where-Object { $_ -like "ADMIN_TOKEN=*" } | Select-Object -First 1
if ($tokenLine) {
    Say "    API Token：$($tokenLine.Substring(12))" "DarkGray"
}
Say "    文档：docs/SETUP.md 和 docs/CONFIG.md" "DarkGray"
Say "    停止：Ctrl+C，或双击 stop.bat" "DarkGray"
Say ""
& $venvPy (Join-Path $root "run.py")
