@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo 监听中：现在手动操作手机走一遍「进入活动页 - 看广告 - 领取」。
echo 每切换一个页面，这里会刷出该页所有可识别的文字和坐标。Ctrl+C 结束。
echo.
"%PY%" ui_probe.py --watch 3
pause
