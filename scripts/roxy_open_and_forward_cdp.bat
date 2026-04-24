@echo off
setlocal

if "%~1"=="" (
  echo Usage:
  echo   %~nx0 TOKEN DIR_ID [WORKSPACE_ID] [API_HOST]
  echo Example:
  echo   %~nx0 39d3ed11b093f27f1aeaad355fc6dee9 c26d150d885c29be3a33e83c3693fb5e 98785 http://127.0.0.1:50000
  exit /b 1
)

set "TOKEN=%~1"
set "DIR_ID=%~2"
set "WORKSPACE_ID=%~3"
set "API_HOST=%~4"

if "%DIR_ID%"=="" (
  echo [ERR] DIR_ID is required.
  exit /b 1
)

if "%API_HOST%"=="" set "API_HOST=http://127.0.0.1:50000"

set "PS_ARGS=-ExecutionPolicy Bypass -File "%~dp0roxy_open_and_forward_cdp.ps1" -ApiHost "%API_HOST%" -Token "%TOKEN%" -DirId "%DIR_ID%" -Headless $true -ForceOpen $true"

if not "%WORKSPACE_ID%"=="" (
  set "PS_ARGS=%PS_ARGS% -WorkspaceId %WORKSPACE_ID%"
)

powershell %PS_ARGS%
exit /b %errorlevel%
