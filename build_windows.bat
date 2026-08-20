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

set "SEVENZIP_MODE="

if exist "tools\7zz.exe" (
  set "SEVENZIP_MODE=7zz"
) else (
  if exist "tools\7z.exe" (
    if not exist "tools\7z.dll" (
      echo ERROR: tools\7z.exe exists, but tools\7z.dll is missing.
      echo The standard Windows 7-Zip console executable requires 7z.dll.
      echo Alternatively, place a standalone 7zz.exe in the tools folder.
      exit /b 1
    )
    set "SEVENZIP_MODE=7z"
  )
)

if not defined SEVENZIP_MODE (
  echo ERROR: A Windows 7-Zip console build is required for CBR support.
  echo.
  echo Put one of these in the tools folder:
  echo   tools\7zz.exe
  echo.
  echo Or:
  echo   tools\7z.exe
  echo   tools\7z.dll
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

if exist "tools\ffmpeg-nvenc.exe" (
  copy /Y "tools\ffmpeg-nvenc.exe" "dist\LANStream\ffmpeg-nvenc.exe" >nul
)

if "%SEVENZIP_MODE%"=="7zz" (
  copy /Y "tools\7zz.exe" "dist\LANStream\7zz.exe" >nul
) else (
  copy /Y "tools\7z.exe" "dist\LANStream\7z.exe" >nul
  copy /Y "tools\7z.dll" "dist\LANStream\7z.dll" >nul
)

copy /Y "README-WINDOWS.txt" "dist\LANStream\README-WINDOWS.txt" >nul

if not exist "dist\LANStream\cache" mkdir "dist\LANStream\cache"

echo.
echo Build complete:
echo   %CD%\dist\LANStream
echo.
echo Included 7-Zip backend:
if "%SEVENZIP_MODE%"=="7zz" (
  echo   7zz.exe
) else (
  echo   7z.exe + 7z.dll
)
echo.
echo Run:
echo   dist\LANStream\LANStream.exe
echo.
pause
