@echo off
setlocal
cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
set "SCRIPT=%~dp0wallet_daily.py"

echo Registering scheduled task: XiaomiWalletDaily
echo   start  : daily 07:00 + random delay up to 60 min
echo   python : %PYW%
echo   script : %SCRIPT%
echo.

schtasks /Create /F /SC DAILY /ST 07:00 /TN "XiaomiWalletDaily" /TR "\"%PYW%\" \"%SCRIPT%\""
if errorlevel 1 (
  echo.
  echo FAILED. Right-click this file - Run as administrator, then retry.
  goto :eof
)

rem 每天固定 07:00:00 准点触发本身就是一条时间指纹，加随机延迟打散
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t=Get-ScheduledTask -TaskName 'XiaomiWalletDaily'; $t.Triggers[0].RandomDelay='PT60M'; Set-ScheduledTask -TaskName 'XiaomiWalletDaily' -Trigger $t.Triggers[0] | Out-Null; Write-Host ('RandomDelay: ' + (Get-ScheduledTask -TaskName 'XiaomiWalletDaily').Triggers[0].RandomDelay)"

echo.
echo OK.
echo   Query : schtasks /Query /TN XiaomiWalletDaily
echo   Delete: schtasks /Delete /TN XiaomiWalletDaily /F
echo.
pause
