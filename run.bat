@echo off
REM SPDX-FileCopyrightText: 2026 word3500 contributors
REM SPDX-License-Identifier: GPL-3.0

REM word3500 启动脚本（Windows）
REM 以模块方式运行 api/server.py，确保 "from api import auth" 能被正确解析。
cd /d "%~dp0"
python -m api.server %*
if errorlevel 1 (
    echo.
    echo 启动失败：请确认已安装 Python 3 且位于 PATH 中。
    pause
)
