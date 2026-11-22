@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -m tools.admin_console %*
) else (
  python -m tools.admin_console %*
)
if errorlevel 1 pause
