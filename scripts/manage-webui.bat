@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
set "DRIVER=%SCRIPT_DIR%manage_webui.py"

if not exist "%DRIVER%" (
    echo [ERR] Driver not found: "%DRIVER%"
    exit /b 1
)

set "PYTHON_CMD="
where python.exe >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python.exe"

if not defined PYTHON_CMD (
    where py.exe >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=py.exe -3"
)

if not defined PYTHON_CMD (
    echo [ERR] Python was not found in PATH. Please install Python first.
    exit /b 1
)

%PYTHON_CMD% "%DRIVER%" %*
exit /b %ERRORLEVEL%
