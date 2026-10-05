@echo off
chcp 65001 >nul
set SRV=
if exist "%~dp0server.txt" set /p SRV=<"%~dp0server.txt"
if defined SRV (echo Current server IP: %SRV% & set /p NEW=Press Enter to keep it, or type a new IP: ) else (set /p NEW=Server IP, e.g. 192.168.1.10 : )
if defined NEW set SRV=%NEW%
>"%~dp0server.txt" echo %SRV%
set URL=http://%SRV%:8080
set UD=%LOCALAPPDATA%\IstakozaPOS_Browser
set CH=
for %%P in ("%ProgramFiles%\Google\Chrome\Application\chrome.exe" "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" "%LocalAppData%\Google\Chrome\Application\chrome.exe") do if exist %%P set CH=%%~P
if defined CH goto chrome
start "" msedge --kiosk-printing --user-data-dir="%UD%" --app=%URL%
exit /b
:chrome
start "" "%CH%" --kiosk-printing --user-data-dir="%UD%" --app=%URL%
exit /b
