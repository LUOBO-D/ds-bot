# 启动 qqbot（UTF-8 控制台，方便看中文日志）
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\run.py"
