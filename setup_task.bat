@echo off
setlocal
cd /d "%~dp0"
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"
set "SCRIPT=%~dp0wallet_daily.py"

echo Registering scheduled tasks
echo   [1] XiaomiWalletDaily    daily 07:00 + random delay up to 60 min
echo   [2] XiaomiWalletDailyPM  every 2h 09:00-21:00 + random delay up to 12 min (--sweep)
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
  "$n='XiaomiWalletDaily'; $t=Get-ScheduledTask -TaskName $n; $t.Triggers[0].RandomDelay='PT60M'; Set-ScheduledTask -TaskName $n -Trigger $t.Triggers[0] | Out-Null; Write-Host ('  [1] RandomDelay: ' + (Get-ScheduledTask -TaskName $n).Triggers[0].RandomDelay)"

rem 广告是分批下发的，单轮做不完 3 条，所以白天每 2 小时补检一次，
rem 直到 state.json 里的 ads_done 达到 ad_target（默认 3）就自动跳过
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$wd='%~dp0'; $a=New-ScheduledTaskAction -Execute '%PYW%' -Argument ('\"%~dp0wallet_daily.py\" --sweep') -WorkingDirectory $wd; $t=New-ScheduledTaskTrigger -Once -At 09:00 -RepetitionInterval (New-TimeSpan -Minutes 120) -RepetitionDuration (New-TimeSpan -Hours 12); $t.RandomDelay='PT12M'; $s=New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew; Register-ScheduledTask -TaskName 'XiaomiWalletDailyPM' -Action $a -Trigger $t -Settings $s -Force | Out-Null; Write-Host '  [2] registered, random delay 12 min'"

echo.
echo OK.
echo   Query : schtasks /Query /TN XiaomiWalletDaily ^& schtasks /Query /TN XiaomiWalletDailyPM
echo   Delete: schtasks /Delete /TN XiaomiWalletDaily /F ^& schtasks /Delete /TN XiaomiWalletDailyPM /F
echo.
pause
