@echo off
REM ----------------------------------------------------------------------
REM  Build PawseyUploader.exe for Windows
REM  Run this from any cmd window that has Python on PATH
REM  (your conda 'pawsey' env works fine).
REM ----------------------------------------------------------------------

setlocal

echo.
echo === Pawsey Uploader - Windows build script ===
echo.

REM Make sure we are in the folder this script lives in
cd /d "%~dp0"

REM 1. Check Python is available
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo Activate your conda env first:  conda activate pawsey
    pause
    exit /b 1
)

python --version

REM 2. Install / upgrade PyInstaller quietly
echo.
echo Installing/updating PyInstaller...
python -m pip install --upgrade pyinstaller >nul
if errorlevel 1 (
    echo ERROR: pip install pyinstaller failed.
    pause
    exit /b 1
)

REM 3. Clean previous build artefacts (optional but tidy)
if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist
if exist PawseyUploader.spec del /q PawseyUploader.spec

REM 4. Build
echo.
echo Building PawseyUploader.exe (this takes 1-2 minutes)...
echo.
python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name PawseyUploader ^
    --clean ^
    pawsey_uploader.py

if errorlevel 1 (
    echo.
    echo ERROR: build failed. See output above.
    pause
    exit /b 1
)

echo.
echo ======================================================================
echo  Build complete.
echo  Your executable is at:   %cd%\dist\PawseyUploader.exe
echo ======================================================================
echo.
echo You can copy that single .exe anywhere - it does NOT need Python
echo installed on the target machine.  BUT note: rclone must still be on
echo PATH on the machine where you run the .exe (the app calls rclone).
echo.
pause
endlocal
