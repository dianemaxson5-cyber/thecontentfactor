@echo off
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python is not installed. Download it from https://www.python.org/downloads/
  echo During setup, tick the box "Add python.exe to PATH".
  pause
  exit /b 1
)
echo Getting things ready. The first time takes about a minute...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
python app.py
pause
