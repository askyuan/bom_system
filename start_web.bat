@echo off
REM ============================================================
REM  ?????? - Web ????? (Windows)
REM  ??: start_web.bat [--host 0.0.0.0] [--port 5000] [--debug]
REM  ????: BOM_HOST, BOM_PORT ??????
REM ============================================================

cd /d "%~dp0"

set "PYTHON=%~dp0venv\Scripts\python.exe"

if not exist "%PYTHON%" (
    echo ????????????? setup.bat
    pause
    exit /b 1
)

if "%BOM_HOST%"=="" set "BOM_HOST=0.0.0.0"
if "%BOM_PORT%"=="" set "BOM_PORT=5000"

echo ============================================================
echo   ?????? - Web ??
echo ============================================================
echo   ??: http://%BOM_HOST%:%BOM_PORT%
echo   ???: %~dp0
echo   ? Ctrl+C ????
echo ============================================================

"%PYTHON%" "%~dp0web.py" --host %BOM_HOST% --port %BOM_PORT% %*
