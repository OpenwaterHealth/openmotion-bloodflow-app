@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   Building Combined BloodFlow Test Runner
echo ============================================================
echo.

:: Check Python is available
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Make sure Python is installed and in PATH.
    pause
    exit /b 1
)

:: Install / upgrade PyInstaller
echo Checking PyInstaller...
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo Installing PyInstaller...
    pip install pyinstaller
    if errorlevel 1 (
        echo ERROR: Failed to install PyInstaller.
        pause
        exit /b 1
    )
) else (
    echo PyInstaller already installed.
)
echo.

:: Clean previous build artefacts
echo Cleaning previous build...
if exist "dist\HappyPathTestRunner.exe" del /f /q "dist\HappyPathTestRunner.exe"
if exist "build"                        rmdir /s /q "build"
if exist "HappyPathTestRunner.spec"      del /f /q "HappyPathTestRunner.spec"
echo.

:: ── Build ────────────────────────────────────────────────────────────────────
:: --onefile        : single portable .exe
:: --console        : keep console window so test output is visible
:: --collect-all    : bundle every sub-module of pywinauto and comtypes
::                    (needed because they use heavy dynamic/COM loading)
:: --hidden-import  : explicitly tell PyInstaller about the four test modules
::                    (they are imported inside functions, so auto-detection
::                     may miss them)
:: --add-data       : include the .py source files in the bundle so they can
::                    be imported at runtime (sys._MEIPASS is on sys.path)
:: ─────────────────────────────────────────────────────────────────────────────
echo Building exe (this may take a minute)...
echo.

pyinstaller ^
    --onefile ^
    --console ^
    --name HappyPathTestRunner ^
    --collect-all pywinauto ^
    --collect-all comtypes ^
    --hidden-import SubjectIDwithJson ^
    --hidden-import Notes ^
    --hidden-import Sensorduration ^
    --hidden-import Analyze ^
    --add-data "SubjectIDwithJson.py;." ^
    --add-data "Notes.py;." ^
    --add-data "Sensorduration.py;." ^
    --add-data "Analyze.py;." ^
    combined_runner.py

if errorlevel 1 (
    echo.
    echo ============================================================
    echo   BUILD FAILED.  Check the output above for details.
    echo ============================================================
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   BUILD SUCCESSFUL
echo   Exe location: dist\HappyPathTestRunner.exe
echo.
echo   To run the tests:
echo     1. Copy dist\HappyPathTestRunner.exe to the same folder
echo        as OpenWaterApp.exe
echo     2. Double-click HappyPathTestRunner.exe
echo     3. Results are saved to HappyPath_test_report.json
echo        in the same folder as the exe
echo ============================================================
pause
