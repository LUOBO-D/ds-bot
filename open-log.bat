@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 大肥鱼 - 后台日志
echo 正在显示 logs\qqbot.log（Ctrl+C 退出）...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -NoExit -Command "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Content -LiteralPath 'logs\qqbot.log' -Wait -Tail 80 -Encoding UTF8"
