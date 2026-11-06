@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt pyinstaller
if errorlevel 1 ( pause & exit /b 1 )
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed --name Medkarta --add-data "ui;ui" --add-data "reference;reference" --add-data "templates;templates" --collect-data pdfminer --collect-all pypdfium2 app.py
if errorlevel 1 ( pause & exit /b 1 )
copy /Y "flash\portable.txt" "dist\Medkarta\portable.txt" >nul
if exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" (
  xcopy /E /I /Y /Q "%ProgramFiles%\Tesseract-OCR" "dist\Medkarta\tesseract" >nul
  echo Tesseract OCR copied into dist\Medkarta\tesseract
) else (
  echo Tesseract not found - this build will work without OCR. See README.
)
echo.
echo Done: dist\Medkarta  (clean build, no personal data inside)
echo To update the USB drive: copy Medkarta.exe and the _internal folder over the old ones.
echo Never overwrite the data folder or settings.json on the USB drive.
pause
