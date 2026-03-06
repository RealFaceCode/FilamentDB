@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%start_all_services.ps1"

if not exist "%PS_SCRIPT%" (
  echo PowerShell script not found: %PS_SCRIPT%
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" -Proxy lan -IncludeSlotPoller %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo Start failed with exit code %EXITCODE%.
)

exit /b %EXITCODE%
