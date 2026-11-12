@echo off
REM CFWives Chat Stream Exporter - Windows launcher
REM Uses the system Python that has Playwright installed

set SCRIPT_DIR=%~dp0
set PYTHON="C:/Users/emby/AppData/Local/Programs/Python/Python314/python.exe"

if not exist %PYTHON% (
    echo ERROR: Python not found at C:/Users/emby/AppData/Local/Programs/Python/Python314/python.exe
    echo Please edit run.bat to point to your Python installation.
    pause
    exit /b 1
)

REM Pass all arguments through. Add --cookies path if you saved cookies:
REM   run.bat --cookies cfwives_cookies.json --limit 100 --interval 10
%PYTHON% "%SCRIPT_DIR%main.py" %*
pause
