@echo off
rem Windows: double-click to start the web UI on this computer and open it in the browser.
cd /d "%~dp0"
call run.bat --server --host 127.0.0.1 --port 8080 --open-browser
