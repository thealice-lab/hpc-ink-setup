@echo off
setlocal
rem Run the pinned Windows test-build downloader; no administrator rights needed.
set "PS_SCRIPT=%~dp0scripts\windows\get_inklydesktop_test.ps1"
if not exist "%PS_SCRIPT%" set "PS_SCRIPT=%~dp0get_inklydesktop_test.ps1"
if not exist "%PS_SCRIPT%" (
  echo ERROR: get_inklydesktop_test.ps1 is missing.
  echo Keep this .cmd next to the .ps1, or run it from the Inkly repository root.
  pause
  exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%PS_SCRIPT%" %*
set "STATUS=%ERRORLEVEL%"
if not "%STATUS%"=="0" echo Inkly Desktop preparation failed. See the error above.
echo.
pause
exit /b %STATUS%
