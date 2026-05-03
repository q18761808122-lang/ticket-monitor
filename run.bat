@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"
REM 票务监控 — 后台静默模式
start "" /B pythonw.exe ticket_monitor.py >nul 2>&1
