@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
)

start "Dies Natalis Photobooth Server" cmd /k "uvicorn app:app --host 0.0.0.0 --port 8000"

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