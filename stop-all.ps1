# 停止 qqbot 相关进程：桥接 → QQ 空间 sidecar（-WithQQ 连 QQ/NapCat 一起关）
#
# 用法：
#   .\stop-all.ps1            # 只关桥接和 sidecar
#   .\stop-all.ps1 -WithQQ    # 连 QQ 一起关掉（NapCat 也就没了）
#
[CmdletBinding()]
param(
    [switch]$WithQQ
)

$ErrorActionPreference = "Continue"

Write-Host ""
Write-Host "===== qqbot 一键停止 =====" -ForegroundColor Cyan

# 1. 桥接
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*run.py*" -and $_.CommandLine -like "*qq_bot*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  已停止桥接 python pid=$($_.ProcessId)" -ForegroundColor Green
        $killed++
    }
if (-not $killed) { Write-Host "  桥接没在跑" -ForegroundColor DarkGray }

# 2. sidecar（node 跑的是 services\onebot-qzone 里的 tsx）
$killed = 0
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
    Where-Object { $_.CommandLine -like "*onebot-qzone*" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "  已停止 sidecar node pid=$($_.ProcessId)" -ForegroundColor Green
        $killed++
    }
if (-not $killed) { Write-Host "  sidecar 没在跑" -ForegroundColor DarkGray }

# 3. QQ（可选）
if ($WithQQ) {
    $qq = Get-Process QQ -ErrorAction SilentlyContinue
    if ($qq) {
        $qq | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Host "  已关闭 QQ（$($qq.Count) 个进程，NapCat 随之退出）" -ForegroundColor Green
    } else {
        Write-Host "  QQ 没在跑" -ForegroundColor DarkGray
    }
}

Write-Host "  完成。" -ForegroundColor Cyan
