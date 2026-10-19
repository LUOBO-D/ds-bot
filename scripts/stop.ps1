# ds-bot 停止（Windows）
# 用法：双击 stop.bat，或 powershell -File scripts\stop.ps1 -All

param([switch]$All)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }

Say "==> 停止桥接服务" "Cyan"
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*run.py*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed += 1
        Say "    已停止 python.exe (PID $($_.ProcessId))" "DarkGray"
    }
if ($killed -eq 0) { Say "    没有在运行的桥接进程" "DarkGray" }

if ($All) {
    Say "==> 停止 QQ / NapCat" "Cyan"
    Get-Process QQ -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Say "    已停止 QQ.exe (PID $($_.Id))" "DarkGray"
    }
}

Say "[+] 完成" "Green"
