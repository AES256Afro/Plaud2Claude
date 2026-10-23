@echo off
rem Plaud2Claude collector launcher for Windows. "collect provision ..." runs provision.py; anything else
rem ("probe", "sync", "push ...") goes to collect.py. It uses the virtual environment in .venv next to this
rem file when there is one (python -m venv .venv && .venv\Scripts\pip install -r requirements.txt).
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
if /I "%~1"=="provision" (
  shift
  "%PY%" "%HERE%provision.py" %1 %2 %3 %4 %5 %6 %7 %8 %9
) else (
  "%PY%" "%HERE%collect.py" %*
)
endlocal
