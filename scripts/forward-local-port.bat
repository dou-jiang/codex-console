@echo off
setlocal

if "%~1"=="" (
  echo Usage:
  echo   %~nx0 PORT [TARGET_PORT] [TARGET_ADDRESS]
  echo Example:
  echo   %~nx0 50000
  echo   %~nx0 9222 9222 127.0.0.1
  exit /b 1
)

set "PORT=%~1"
set "TARGET_PORT=%~2"
set "TARGET_ADDRESS=%~3"

if "%TARGET_PORT%"=="" set "TARGET_PORT=%PORT%"
if "%TARGET_ADDRESS%"=="" set "TARGET_ADDRESS=127.0.0.1"

powershell -ExecutionPolicy Bypass -File "%~dp0forward-local-port.ps1" -Port %PORT% -TargetPort %TARGET_PORT% -TargetAddress "%TARGET_ADDRESS%"
exit /b %errorlevel%
