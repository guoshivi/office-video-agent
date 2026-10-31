@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if %errorlevel%==0 (
        py -3 -m venv .venv
    ) else (
        python -m venv .venv
    )
    if errorlevel 1 goto :error
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :error
)
if not exist data mkdir data
if "%OVA_HOST%"=="" set "OVA_HOST=127.0.0.1"
if "%OVA_PORT%"=="" set "OVA_PORT=8770"
start "Office Video Agent" http://%OVA_HOST%:%OVA_PORT%
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %OVA_HOST% --port %OVA_PORT%
goto :eof
:error
echo Setup failed. Check Python and network access, then run this file again.
pause
exit /b 1
