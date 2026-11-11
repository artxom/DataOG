@echo off
setlocal
cd /d "%~dp0"

echo ============================================================================
echo   DataOG v2.0.0 Workbench - Windows Standalone EXE Builder
echo ============================================================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] python was not found in PATH. Please install Python 3.10+ first.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [STEP 1] Creating virtual environment .venv ...
    python -m venv .venv
    if %errorlevel% neq 0 (
        echo [WARN] venv creation failed, falling back to system python...
        goto :USE_SYS_PYTHON
    )
)

echo [STEP 2] Installing dependencies into .venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies from requirements.txt.
    pause
    exit /b 1
)

echo [STEP 3] Building standalone DataOG.exe via build_exe.py ...
".venv\Scripts\python.exe" build_exe.py
if %errorlevel% neq 0 (
    echo [ERROR] Packaging failed. Please check the error output above.
    pause
    exit /b 1
)
goto :DONE

:USE_SYS_PYTHON
echo [STEP 2] Installing dependencies using system python ...
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies.
    pause
    exit /b 1
)
echo [STEP 3] Building standalone DataOG.exe via build_exe.py ...
python build_exe.py
if %errorlevel% neq 0 (
    echo [ERROR] Packaging failed.
    pause
    exit /b 1
)

:DONE
echo.
echo [SUCCESS] Build finished! Check dist\DataOG\DataOG.exe.
pause
