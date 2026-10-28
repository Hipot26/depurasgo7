@echo off
chcp 65001 >nul
title SGS UI — Servidor Local

echo.
echo ============================================================
echo   SGS UI — Servidor Local
echo   http://127.0.0.1:5001
echo ============================================================
echo.

:: Ir a la carpeta del script
cd /d "%~dp0"

:: Python 3.14 — tiene playwright y flask instalados
set PYTHON=C:\Users\duran\AppData\Local\Programs\Python\Python314\python.exe

if not exist "%PYTHON%" (
    echo [ERROR] No se encontro Python 3.14 en:
    echo         %PYTHON%
    pause
    exit /b 1
)

echo [1/2] Verificando dependencias...
"%PYTHON%" -m pip install -q -r requirements.txt

echo [2/2] Iniciando servidor en http://127.0.0.1:5001 ...
echo.
echo   Presiona Ctrl+C para detener el servidor.
echo.

:: Abrir el navegador después de 2 segundos (en background)
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:5001"

:: Arrancar Flask
"%PYTHON%" app.py

echo.
echo Servidor detenido.
pause
