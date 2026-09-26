@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 "%~dp0workbuddy_transfer.py" %*
) else (
  python "%~dp0workbuddy_transfer.py" %*
)
exit /b %errorlevel%

