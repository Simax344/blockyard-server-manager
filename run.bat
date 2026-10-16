@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 app.py
) else (
  python app.py
)
if errorlevel 1 (
  echo.
  echo Blockyard could not start. See the error above. Install Python 3.10 or newer if Python is missing.
  pause
)
endlocal
