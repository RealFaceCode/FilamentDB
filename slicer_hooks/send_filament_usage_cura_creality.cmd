@echo off
setlocal EnableExtensions
call "%~dp0send_filament_usage.cmd" %*
exit /b %ERRORLEVEL%
