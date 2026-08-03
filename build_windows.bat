@echo off
setlocal
cd /d "%~dp0"

echo.
echo LANStream Windows build
echo =======================
echo.

if not exist "tools\ffmpeg.exe" (
  echo ERROR: tools\ffmpeg.exe is missing.
  echo Put a Windows FFmpeg build in the tools folder before building.
  exit /b 1
)

if not exist "tools\ffprobe.exe" (
  echo ERROR: tools\ffprobe.exe is missing.
  echo Put ffprobe.exe from the same FFmpeg build in the tools folder.
  exit /b 1
)

if not exist ".venv-build\Scripts\python.exe" (
  py -3 -m venv .venv-build
  if errorlevel 1 exit /b 1
)

call ".venv-build\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1

python -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1

python -m PyInstaller --clean --noconfirm LANStream.spec
if errorlevel 1 exit /b 1

copy /Y "config.txt" "dist\LANStream\config.txt" >nul
copy /Y "tools\ffmpeg.exe" "dist\LANStream\ffmpeg.exe" >nul
copy /Y "tools\ffprobe.exe" "dist\LANStream\ffprobe.exe" >nul
copy /Y "README-WINDOWS.txt" "dist\LANStream\README-WINDOWS.txt" >nul

if not exist "dist\LANStream\cache" mkdir "dist\LANStream\cache"

echo.
echo Build complete:
echo   %CD%\dist\LANStream
echo.
echo Run:
echo   dist\LANStream\LANStream.exe
echo.
pause
