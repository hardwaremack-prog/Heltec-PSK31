@echo off
title LoPSK
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
%PY% --version >nul 2>nul || (
  echo Python 3 is needed. Get it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
  pause
  exit /b 1
)
%PY% -c "import serial, esptool, numpy, sounddevice" >nul 2>nul || (
  echo Installing LoPSK requirements, one moment...
  %PY% -m pip install --quiet --upgrade pyserial esptool numpy sounddevice
)
%PY% -c "import rtlsdr" >nul 2>nul || %PY% -m pip install --quiet "pyrtlsdr[lib]" >nul 2>nul
start "" %PY%w lopsk_app.py 2>nul || %PY% lopsk_app.py
