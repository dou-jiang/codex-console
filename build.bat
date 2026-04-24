@echo off
setlocal
setlocal enabledelayedexpansion

echo === Build platform: Windows ===

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found in PATH.
    exit /b 1
)

if exist requirements.txt (
    echo Installing project dependencies...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Failed to install project dependencies.
        exit /b 1
    )
)

echo Installing PyInstaller...
python -m pip install pyinstaller --quiet
if errorlevel 1 (
    echo Failed to install PyInstaller.
    exit /b 1
)

echo Running PyInstaller...
python -m PyInstaller codex_register.spec --clean --noconfirm
if errorlevel 1 (
    echo PyInstaller build failed.
    exit /b 1
)

if exist dist\codex-console.exe (
    for /f "tokens=*" %%i in ('powershell -Command "[System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture"') do set ARCH=%%i
    set OUTPUT=dist\codex-console-windows-!ARCH!.exe
    if exist "!OUTPUT!" del /F /Q "!OUTPUT!" >nul 2>nul
    move /Y dist\codex-console.exe "!OUTPUT!" >nul
    if errorlevel 1 (
        echo === Build complete, but final rename was blocked. ===
        echo Close any running copy of !OUTPUT! and rename dist\codex-console.exe manually if needed.
        echo Fresh binary is available at: dist\codex-console.exe
        exit /b 0
    )
    echo === Build complete: !OUTPUT! ===
) else (
    echo === Build failed: dist\codex-console.exe not found ===
    exit /b 1
)
