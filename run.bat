@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem The .venv is tied to the Python it was created with. If that Python is gone (other PC, reinstall), rebuild it.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1 || (
    echo The .venv folder belongs to another Python installation. Recreating it...
    rmdir /s /q .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating .venv ...
  py -3.14 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Could not create .venv. Is Python 3.14 installed? Check: py -3.14 --version
  pause
  exit /b 1
)
rem Install libraries only when requirements.txt has changed since the last install
fc /b requirements.txt ".venv\installed-requirements.txt" >nul 2>&1
if errorlevel 1 (
  echo Installing libraries - first run or new requirements, may take a few minutes...
  ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --progress-bar on -r requirements.txt
  if errorlevel 1 (
    echo Library install failed, see messages above.
    pause
    exit /b 1
  )
  copy /Y requirements.txt ".venv\installed-requirements.txt" >nul
)
".venv\Scripts\python.exe" app.py
if errorlevel 1 (
  echo.
  echo The program stopped with an error, see messages above.
  pause
)
