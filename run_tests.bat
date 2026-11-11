@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import pytest" >nul 2>nul
    if %errorlevel% equ 0 (
        ".venv\Scripts\python.exe" -m pytest %*
        exit /b %errorlevel%
    )
    ".venv\Scripts\python.exe" -m unittest discover -v -s tests
    exit /b %errorlevel%
)

python -c "import pytest" >nul 2>nul
if %errorlevel% equ 0 (
    python -m pytest %*
    exit /b %errorlevel%
)

python -m unittest discover -v -s tests
exit /b %errorlevel%
