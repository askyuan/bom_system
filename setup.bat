@echo off
REM ============================================================
REM  ?????? - ?????? (Windows)
REM  ??: setup.bat [--skip-init] [--force]
REM ============================================================

cd /d "%~dp0"

echo ??????????...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if %errorlevel% neq 0 (
    echo.
    echo ???????????????
    pause
    exit /b 1
)

pause
