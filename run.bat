@echo off
setlocal
cd /d "%~dp0"

if "%PYTHON%"=="" set "PYTHON=python"

if not exist .venv (
  echo Creating virtualenv...
  %PYTHON% -m venv .venv
  if errorlevel 1 (
    echo Failed to create venv. Make sure Python 3.10+ is installed and on PATH.
    exit /b 1
  )
)

.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

if "%PFB_HOST%"=="" set "PFB_HOST=127.0.0.1"
if "%PFB_PORT%"=="" set "PFB_PORT=8090"

echo Starting Prototype Feedback server at http://%PFB_HOST%:%PFB_PORT%
.venv\Scripts\python.exe -m uvicorn main:app --host %PFB_HOST% --port %PFB_PORT% --reload
