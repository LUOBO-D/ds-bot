@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo.
echo ============================================
echo   ds-bot 一键安装
echo ============================================
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
echo.
pause
