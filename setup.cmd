@echo off
setlocal
cd /d "%~dp0"
echo.
echo  Voice agent setup
echo  -----------------
where python >nul 2>nul
if errorlevel 1 goto :nopython
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 goto :oldpython

echo [1/4] Installing the voice agent into .venv ...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
if errorlevel 1 goto :fail

echo [2/4] Installing screen control (windows-mcp) ...
if not exist "voice-agent\.venv-wm\Scripts\python.exe" python -m venv "voice-agent\.venv-wm"
if errorlevel 1 goto :fail
"voice-agent\.venv-wm\Scripts\python.exe" -m pip install --quiet --upgrade pip
"voice-agent\.venv-wm\Scripts\python.exe" -m pip install --quiet windows-mcp==0.8.5
if errorlevel 1 goto :fail

echo [3/4] Downloading the speech models - about 540 MB, one time only ...
".venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('tiny.en', device='cpu', compute_type='int8'); WhisperModel('small.en', device='cpu', compute_type='int8'); print('      models ready')"
if errorlevel 1 goto :fail

echo [4/4] Checking for Claude Code ...
where claude >nul 2>nul
if errorlevel 1 goto :noclaude
echo       found. If you have never used it, run  claude  once in a terminal and log in.
goto :done

:noclaude
echo       not found. Instant commands will work without it. For multi-step requests
echo       and research, install Claude Code from https://claude.com/claude-code
echo       then run  claude  once in a terminal and log in.

:done
echo.
echo  Done. Double-click "Start Voice Agent.cmd" to run it.
echo.
pause
exit /b 0

:nopython
echo Python was not found. Install Python 3.12 or newer from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer, then run setup.cmd again.
pause
exit /b 1

:oldpython
echo This needs Python 3.11 or newer. Install a newer Python from https://www.python.org/downloads/
pause
exit /b 1

:fail
echo.
echo Setup stopped because of the error above. Fix it and run setup.cmd again -
echo it picks up where it left off.
pause
exit /b 1
