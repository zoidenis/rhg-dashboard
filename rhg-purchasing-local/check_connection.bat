@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run start.bat once first. & pause & exit /b 1)
".venv\Scripts\python.exe" check_bc.py
