@echo off
title RHG Purchasing Dashboard
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed or not on PATH. Install Python 3.10+ from python.org and tick "Add to PATH".
  pause & exit /b 1
)

if not exist ".env" (
  copy ".env.example" ".env" >nul
  echo Created .env - fill in BC_USERNAME and BC_PASSWORD, save, then run start.bat again.
  notepad ".env"
  exit /b 0
)

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up...
  python -m venv .venv || (echo Could not create virtual environment & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || (echo Package install failed & pause & exit /b 1)
)

".venv\Scripts\python.exe" server.py
pause
