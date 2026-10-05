@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Builds the 64bit installer ONLY. Needs NSIS: https://nsis.sourceforge.io
set NSIS=%ProgramFiles(x86)%\NSIS\makensis.exe
if not exist "%NSIS%" set NSIS=%ProgramFiles%\NSIS\makensis.exe
if not exist "%NSIS%" (echo NSIS not found. Install it from https://nsis.sourceforge.io & pause & exit /b 1)
"%NSIS%" setup.nsi || goto err
echo.
echo DONE: IstakozaPOS_Setup_64bit.exe
pause
exit /b 0
:err
echo BUILD FAILED - see messages above.
pause
exit /b 1
