@echo off
chcp 65001 >nul
title Abrir SGS UI

cd /d "%~dp0"

set URL=http://127.0.0.1:5001/

for /f "tokens=5" %%p in ('netstat -ano ^| findstr /R /C:":5001 .*LISTENING"') do (
    start "" "%URL%"
    exit /b 0
)

start "" "%~dp0iniciar_servidor.bat"
exit /b 0
