@echo off
setlocal
cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
set "SCRIPT=%~dp0wallet_daily.py"

echo Registering scheduled task: XiaomiWalletDaily, daily at 08:10
echo   python : %PYW%
echo   script : %SCRIPT%
echo.

schtasks /Create /F /SC DAILY /ST 08:10 /TN "XiaomiWalletDaily" /TR "\"%PYW%\" \"%SCRIPT%\""
if errorlevel 1 (
  echo.
  echo FAILED. Right-click this file - Run as administrator, then retry.
) else (
  echo.
  echo OK.
  echo   Query : schtasks /Query /TN XiaomiWalletDaily
  echo   Delete: schtasks /Delete /TN XiaomiWalletDaily /F
)
echo.
pause
