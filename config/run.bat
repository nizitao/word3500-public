@echo off
REM SPDX-FileCopyrightText: 2026 word3500 contributors
REM SPDX-License-Identifier: GPL-3.0

rem word3500 launcher (legacy) - 推荐直接用项目根目录的 run.bat
rem 说明：server.py 启动后会自动打开浏览器（--no-browser 可关闭），
rem       这里不再提前 start 浏览器，避免打开一个连不上的页面 + 重复标签页。
cd /d "%~dp0.."
if not exist "api\server.py" (
    echo [ERROR] api\server.py not found
    pause
    exit /b 1
)
python -m api.server %*
pause
