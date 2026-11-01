@echo off
rem ===========================================================================
rem  One-click start. Double-click this file.
rem
rem  Keep this file ASCII-only: cmd.exe reads .bat in the system ANSI codepage,
rem  so UTF-8 Chinese here breaks command parsing. All Chinese text the user
rem  sees lives in the web UI, not in this script.
rem ===========================================================================
setlocal
cd /d "%~dp0"

rem ---- first run: create the virtualenv and install dependencies
if not exist ".venv\Scripts\python.exe" (
    echo Preparing the Python environment, this only happens once...
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

rem ---- already running? bring the browser back instead of starting twice
netstat -ano | findstr ":%OVA_PORT%" | findstr "LISTENING" >nul 2>nul
if %errorlevel%==0 (
    echo Already running, opening the browser...
    start "" http://%OVA_HOST%:%OVA_PORT%
    exit /b 0
)

rem ---- open the browser a few seconds later, so nobody sees "can't connect yet"
start "" /min ".venv\Scripts\python.exe" -c "import time,webbrowser;time.sleep(3);webbrowser.open('http://%OVA_HOST%:%OVA_PORT%')"

rem The app starts the publishing helpers (Xiaohongshu service / the Chrome
rem window) by itself once those platforms are switched on, so no extra
rem windows need to be opened here.
".venv\Scripts\python.exe" -m uvicorn app.main:app --host %OVA_HOST% --port %OVA_PORT%
goto :eof

:error
echo.
echo Setup failed. Check that Python 3.10 or newer is installed, and that this
echo computer can reach the internet to install the dependencies.
echo.
pause
exit /b 1
