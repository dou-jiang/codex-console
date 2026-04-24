@echo off
setlocal
cd /d "%~dp0"

set "PS_CMD="
where powershell.exe >nul 2>nul
if not errorlevel 1 set "PS_CMD=powershell.exe"
if not defined PS_CMD (
    where pwsh.exe >nul 2>nul
    if not errorlevel 1 set "PS_CMD=pwsh.exe"
)
if not defined PS_CMD (
    echo.
    echo PowerShell not found. Install Windows PowerShell or PowerShell 7 first.
    exit /b 1
)

%PS_CMD% -ExecutionPolicy Bypass -File ".\scripts\make_windows_test_bundle.ps1"
if errorlevel 1 (
    echo.
    echo Failed to create test bundle.
    exit /b 1
)

echo.
echo Test bundle created successfully.
echo You can run: ..\codex-console2-win-test\start-webui.bat
