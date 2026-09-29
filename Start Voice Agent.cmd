@echo off
title Voice agent
cd /d "%~dp0voice-agent"
if exist "..\.venv\Scripts\python.exe" ("..\.venv\Scripts\python.exe" start.py %*) else (python start.py %*)
pause
