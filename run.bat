@echo off
rem Starts the web UI (or a batch run: --run, --configure, see --help) on Windows. The first start creates a virtual environment in
rem .venv and installs the dependencies into it; they are updated whenever requirements.txt changes.
rem Needs Python 3.10 or newer (https://www.python.org/downloads/ - tick "Add python.exe to PATH").
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto deps
set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo LLM Probe needs Python 3.10 or newer, but none was found.
    echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH", then start this again.
    pause
    exit /b 1
)
echo First start: creating the Python environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 (
    rmdir /s /q .venv 2>nul
    echo Could not create the Python environment.
    pause
    exit /b 1
)

:deps
rem reinstall when requirements.txt is newer than the marker (or the marker is missing)
set "INSTALL="
if not exist ".venv\.installed" set "INSTALL=1"
if exist ".venv\.installed" (
    for /f %%i in ('powershell -NoProfile -Command "if ((Get-Item requirements.txt).LastWriteTime -gt (Get-Item .venv\.installed).LastWriteTime) { 1 }"') do set "INSTALL=1"
)
if defined INSTALL (
    echo Installing the dependencies ^(needs internet access, takes a minute^) ...
    ".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo Installing the dependencies failed - check the internet connection and start this again.
        pause
        exit /b 1
    )
    type nul > ".venv\.installed"
)
".venv\Scripts\python.exe" -m llm_probe %*
