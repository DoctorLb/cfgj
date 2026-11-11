@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if not errorlevel 1 (
    py -3 release.py
) else (
    python release.py
)

if errorlevel 1 (
    echo.
    echo Failed to start. Install 64-bit Python 3 and enable "Add python.exe to PATH".
    pause
)
