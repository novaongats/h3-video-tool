@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
title H3 Video Maker
python "h3_tool\server.py"
pause
