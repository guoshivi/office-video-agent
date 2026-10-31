@echo off
rem Wrapper so nobody has to remember the venv python path: ova status / ova send video.mp4
rem Keep this file ASCII-only: cmd.exe reads .bat in the system ANSI codepage,
rem UTF-8 Chinese here breaks command parsing.
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo [ERROR] venv not found. Run start.bat first.
  exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%tools\ova.py" %*
