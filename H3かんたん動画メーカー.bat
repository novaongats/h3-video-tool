@echo off
chcp 65001 > nul
title H3かんたん動画メーカー
cd /d "%~dp0"
python "h3_tool\server.py"
pause
