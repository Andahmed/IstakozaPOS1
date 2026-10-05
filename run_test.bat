@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem Test WITHOUT installing (bundled Python 64bit). Data goes to the "data" folder here.
set ISTAKOZA_DATA=%~dp0data
py64\python.exe server.py
pause
