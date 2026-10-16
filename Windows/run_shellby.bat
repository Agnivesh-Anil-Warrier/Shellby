@echo off
setlocal enabledelayedexpansion

:: ============================================================================
:: Shellby - Windows Command Prompt Launcher
:: ============================================================================

:: 1. Check for Python virtual environment in standard locations
if exist "%~dp0..\.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0..\.venv\Scripts\python.exe"
) else if exist "%~dp0.venv\Scripts\python.exe" (
    set "PY_EXE=%~dp0.venv\Scripts\python.exe"
) else if exist "%VIRTUAL_ENV%\Scripts\python.exe" (
    set "PY_EXE=%VIRTUAL_ENV%\Scripts\python.exe"
) else (
    set "PY_EXE=python"
)

:: 2. Handle subcommands
if "%~1"=="" goto help
if /i "%~1"=="record" goto record
if /i "%~1"=="gui" goto gui
if /i "%~1"=="guide" goto guide
if /i "%~1"=="curate" goto curate
if /i "%~1"=="pdf" goto pdf
if /i "%~1"=="help" goto help
if /i "%~1"=="--help" goto help
if /i "%~1"=="-h" goto help

:: Forward arbitrary arguments to shellby.py
"%PY_EXE%" "%~dp0shellby.py" %*
goto end

:record
shift
"%PY_EXE%" "%~dp0shellby.py" record %*
goto end

:gui
shift
if exist "%~dp0shellby_qt2.py" (
    "%PY_EXE%" "%~dp0shellby_qt2.py" %*
) else (
    "%PY_EXE%" "%~dp0shellby_qt.py" %*
)
goto end

:guide
shift
"%PY_EXE%" "%~dp0shellby.py" guide %*
goto end

:curate
shift
"%PY_EXE%" "%~dp0shellby.py" curate %*
goto end

:pdf
shift
"%PY_EXE%" "%~dp0shellby.py" pdf %*
goto end

:help
echo.
echo ============================================================================
echo Shellby for Windows Command Prompt
echo ============================================================================
echo.
echo Usage:
echo   run_shellby.bat record [--title "My Task"] [--no-guide]
echo   run_shellby.bat gui
echo   run_shellby.bat guide ^<path_to_session.json^>
echo   run_shellby.bat curate ^<path_to_blocks.json^>
echo   run_shellby.bat pdf ^<path_to_guide.md^>
echo.
echo Examples:
echo   run_shellby.bat record --title deployment_guide
echo   run_shellby.bat gui
echo.
goto end

:end
endlocal
