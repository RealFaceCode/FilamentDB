@echo off
setlocal EnableExtensions EnableDelayedExpansion

REM 0 = Fenster schließt sofort (Standard), 1 = Fenster bleibt am Ende offen
set "KEEP_OPEN=0"

set "EXIT_CODE=0"

call :resolve_file_argument %*
if not defined FILE (
  echo [FILAMENT_DB] ERROR: no valid file argument received
  set "EXIT_CODE=2"
  goto :finish
)

set "URL=https://192.168.127.78:8443/api/usage/auto-from-file"
set "PROJECT=private"
set "DRYRUN=0"
set "RESP_FILE=%TEMP%\filament_usage_response.json"
set "HTTP_STATUS="
set "RESOLVED_FILE="
set "CURL_FAILED=0"

REM Wenn Basic Auth aktiv: AUTH auf Benutzer:Passwort setzen, z. B. admin:meinpasswort
set "AUTH="

call :resolve_input_file "%FILE%"
if defined RESOLVED_FILE (
  if /I not "%RESOLVED_FILE%"=="%FILE%" (
    echo [FILAMENT_DB] INFO: Metadata-Datei erkannt, nutze Hauptdatei:
    echo [FILAMENT_DB] INFO: %RESOLVED_FILE%
    set "FILE=%RESOLVED_FILE%"
  )
)

if not exist "%FILE%" (
  echo [FILAMENT_DB] ERROR: file not found: %FILE%
  set "EXIT_CODE=3"
  goto :finish
)

call :upload_current
if "%CURL_FAILED%"=="1" (
  echo [FILAMENT_DB] ERROR: upload failed
  set "EXIT_CODE=1"
  goto :finish
)

if not "%HTTP_STATUS%"=="200" (
  echo [FILAMENT_DB] ERROR: API HTTP %HTTP_STATUS%
  if exist "%RESP_FILE%" type "%RESP_FILE%"
  set "EXIT_CODE=4"
  goto :finish
)

findstr /c:"\"ok\":true" "%RESP_FILE%" >nul 2>&1
if errorlevel 1 (
  findstr /c:"\"error\":\"no_grams\"" /c:"\"error\":\"no_grams_bambu_unsliced\"" "%RESP_FILE%" >nul 2>&1
  if not errorlevel 1 (
    set "RETRY_CANDIDATE="
    call :resolve_3mf_fallback "%FILE%"
    if defined RETRY_CANDIDATE (
      if /I not "!RETRY_CANDIDATE!"=="!FILE!" (
        echo [FILAMENT_DB] INFO: no_grams fuer !FILE!
        echo [FILAMENT_DB] INFO: Retry mit 3MF: !RETRY_CANDIDATE!
        set "FILE=!RETRY_CANDIDATE!"
        call :upload_current
        if "!CURL_FAILED!"=="1" (
          echo [FILAMENT_DB] ERROR: retry upload failed
          set "EXIT_CODE=1"
          goto :finish
        )
        if "!HTTP_STATUS!"=="200" (
          findstr /c:"\"ok\":true" "%RESP_FILE%" >nul 2>&1
          if not errorlevel 1 goto :success
        )
      )
    ) else (
      echo [FILAMENT_DB] INFO: no_grams und keine 3MF-Kandidaten gefunden
    )
  )
  echo [FILAMENT_DB] ERROR: API returned ok=false
  if exist "%RESP_FILE%" type "%RESP_FILE%"
  set "EXIT_CODE=5"
  goto :finish
)

:success
echo [FILAMENT_DB] OK: %FILE%

:finish
if "%KEEP_OPEN%"=="1" (
  echo.
  echo [FILAMENT_DB] Fertig. Taste druecken zum Schliessen...
  pause >nul
)

exit /b %EXIT_CODE%

:resolve_file_argument
set "FILE="

if "%~1"=="" goto :eof

:resolve_file_argument_loop
if "%~1"=="" goto :eof

if exist "%~1" (
  set "FILE=%~1"
  goto :eof
)

shift
goto :resolve_file_argument_loop

:resolve_input_file
set "INPUT_FILE=%~1"
set "RESOLVED_FILE="

set "META_CHECK=%INPUT_FILE:\Metadata\=%"
if /I "%META_CHECK%"=="%INPUT_FILE%" goto :eof

for %%I in ("%INPUT_FILE%") do set "META_DIR=%%~dpI"
for %%I in ("%META_DIR%..") do set "JOB_DIR=%%~fI"

call :pick_newest_file "%JOB_DIR%" "3mf"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_DIR%" "bgcode"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_DIR%" "gcode"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_DIR%" "gco"
if defined RESOLVED_FILE goto :eof

for %%I in ("%JOB_DIR%..") do set "JOB_PARENT=%%~fI"
if /I "%JOB_PARENT%"=="%JOB_DIR%" goto :eof

call :pick_newest_file "%JOB_PARENT%" "3mf"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_PARENT%" "bgcode"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_PARENT%" "gcode"
if defined RESOLVED_FILE goto :eof
call :pick_newest_file "%JOB_PARENT%" "gco"
goto :eof

:resolve_3mf_fallback
set "SOURCE_FILE=%~1"
set "RETRY_CANDIDATE="

for %%I in ("%SOURCE_FILE%") do set "SRC_DIR=%%~dpI"
for %%I in ("%SRC_DIR%..") do set "SRC_PARENT=%%~fI"
if /I "%SRC_PARENT%"=="%SRC_DIR%" goto :try_source_dir

if /I not "%SRC_PARENT%"=="%SRC_DIR%" (
  set "RESOLVED_FILE="
  call :pick_newest_file "%SRC_PARENT%" "3mf" "1" "0"
  if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
)
if defined RETRY_CANDIDATE goto :eof

if /I not "%SRC_PARENT%"=="%SRC_DIR%" (
  set "RESOLVED_FILE="
  call :pick_newest_file "%SRC_PARENT%" "3mf"
  if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
)
if defined RETRY_CANDIDATE goto :eof

for %%I in ("%SRC_PARENT%..") do set "SRC_GRAND=%%~fI"
if /I not "%SRC_GRAND%"=="%SRC_PARENT%" (
  set "RESOLVED_FILE="
  call :pick_newest_file "%SRC_GRAND%" "3mf" "1" "0"
  if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
)
if defined RETRY_CANDIDATE goto :eof

if /I not "%SRC_GRAND%"=="%SRC_PARENT%" (
  set "RESOLVED_FILE="
  call :pick_newest_file "%SRC_GRAND%" "3mf"
  if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
)
if defined RETRY_CANDIDATE goto :eof

:try_source_dir
set "RESOLVED_FILE="
call :pick_newest_file "%SRC_DIR%" "3mf" "1" "0"
if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
if defined RETRY_CANDIDATE goto :eof

set "RESOLVED_FILE="
call :pick_newest_file "%SRC_DIR%" "3mf"
if defined RESOLVED_FILE set "RETRY_CANDIDATE=%RESOLVED_FILE%"
goto :eof

:upload_current
set "CURL_FAILED=0"
set "HTTP_STATUS="

if defined AUTH (
  for /f %%S in ('curl.exe -k -sS -X POST "%URL%" -u "%AUTH%" -F "file=@%FILE%" -F "project=%PROJECT%" -F "dry_run=%DRYRUN%" -o "%RESP_FILE%" -w "%%{http_code}"') do set "HTTP_STATUS=%%S"
) else (
  for /f %%S in ('curl.exe -k -sS -X POST "%URL%" -F "file=@%FILE%" -F "project=%PROJECT%" -F "dry_run=%DRYRUN%" -o "%RESP_FILE%" -w "%%{http_code}"') do set "HTTP_STATUS=%%S"
)

if errorlevel 1 set "CURL_FAILED=1"
if not defined HTTP_STATUS set "CURL_FAILED=1"
goto :eof

:pick_newest_file
set "SEARCH_DIR=%~1"
set "SEARCH_EXT=%~2"
set "SKIP_CONFIG=%~3"
set "ALLOW_ANY=%~4"

if "%SKIP_CONFIG%"=="" set "SKIP_CONFIG=0"
if "%ALLOW_ANY%"=="" set "ALLOW_ANY=1"

for /f "delims=" %%F in ('dir /b /a:-d /o:-d "%SEARCH_DIR%\*.%SEARCH_EXT%" 2^>nul') do (
  if not defined RESOLVED_FILE (
    set "CAND_FILE=%%F"
    set "CAND_FULL=%SEARCH_DIR%\%%F"
    if "%SKIP_CONFIG%"=="1" (
      set "CAND_INVALID=0"
      if /I "!CAND_FILE!"==".3mf" set "CAND_INVALID=1"
      echo(!CAND_FILE!| findstr /I "_config" >nul
      if not errorlevel 1 set "CAND_INVALID=1"
      echo(!CAND_FULL!| findstr /I "\Metadata\" >nul
      if not errorlevel 1 set "CAND_INVALID=1"
      if "!CAND_INVALID!"=="0" set "RESOLVED_FILE=%SEARCH_DIR%\%%F"
    ) else (
      set "RESOLVED_FILE=%SEARCH_DIR%\%%F"
    )
  )
)

if not defined RESOLVED_FILE if "%SKIP_CONFIG%"=="1" if "%ALLOW_ANY%"=="1" (
  for /f "delims=" %%F in ('dir /b /a:-d /o:-d "%SEARCH_DIR%\*.%SEARCH_EXT%" 2^>nul') do (
    if not defined RESOLVED_FILE set "RESOLVED_FILE=%SEARCH_DIR%\%%F"
  )
)

goto :eof
