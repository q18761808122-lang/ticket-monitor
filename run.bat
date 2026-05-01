@echo off
chcp 65001 >nul
title 票务监控

cd /d "%~dp0"

echo.
echo ╔════════════════════════════════╗
echo ║    演出票务回流票监控工具     ║
echo ╚════════════════════════════════╝
echo.
echo 按 Ctrl+C 可随时停止
echo.

python ticket_monitor.py

pause
