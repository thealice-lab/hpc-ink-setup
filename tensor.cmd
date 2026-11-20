@echo off
setlocal
where py.exe >nul 2>nul
if errorlevel 1 (
    python "%~dp0scripts\tensor_picker.py" %*
) else (
    py -3 "%~dp0scripts\tensor_picker.py" %*
)
set "result=%errorlevel%"
if not "%result%"=="0" echo The handoff stopped. Read the message above before retrying.
pause
exit /b %result%
