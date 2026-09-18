@echo off
setlocal
cd /d "%~dp0"

rem Prefer the launcher, fall back to python on PATH.
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY where python >nul 2>&1 && set "PY=python"

if not defined PY (
    echo.
    echo Python was not found on this machine.
    echo Install it from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup, then run this again.
    echo.
    pause
    exit /b 1
)

%PY% run.py %*
if errorlevel 1 (
    echo.
    pause
)
