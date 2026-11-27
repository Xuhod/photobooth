@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
) else if exist "..\.venv\Scripts\activate.bat" (
    call "..\.venv\Scripts\activate.bat"
)

if not defined PHOTOBOOTH_SHARE_BASE_URL set "PHOTOBOOTH_SHARE_BASE_URL=https://photos.stellarinium.me"
start "Dies Natalis Photobooth Server" cmd /k "python -B -m uvicorn app:app --host 127.0.0.1 --port 8000 --no-proxy-headers --no-access-log"

:wait_server
timeout /t 1 /nobreak >nul
powershell -Command "if ((Test-NetConnection -ComputerName 127.0.0.1 -Port 8000 -WarningAction SilentlyContinue).TcpTestSucceeded) { exit 0 } else { exit 1 }"
if errorlevel 1 goto wait_server

set "URL=http://localhost:8000"
set "PROFILE_DIR=%TEMP%\photobooth-kiosk-profile"

if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" (
    start "" "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" --kiosk "%URL%" --edge-kiosk-type=fullscreen --no-first-run --user-data-dir="%PROFILE_DIR%"
) else if exist "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" (
    start "" "%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" --kiosk "%URL%" --edge-kiosk-type=fullscreen --no-first-run --user-data-dir="%PROFILE_DIR%"
) else if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" (
    start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" --kiosk "%URL%" --no-first-run --user-data-dir="%PROFILE_DIR%"
) else if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" (
    start "" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" --kiosk "%URL%" --no-first-run --user-data-dir="%PROFILE_DIR%"
) else (
    start "" "%URL%"
)
