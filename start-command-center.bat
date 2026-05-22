@echo off
title Agent Command Center
echo.
echo  ========================================
echo   Agent Command Center - Launcher
echo  ========================================
echo.

cd /d "%~dp0"

:: Start the Python API server in background
start "Command Center API" /min python command-center-api.py

:: Wait a moment for the server to start
timeout /t 2 /nobreak >nul

:: Open the dashboard in the default browser
start http://localhost:9876/

echo  Dashboard opened at http://localhost:9876/
echo  API server running in background.
echo  Close the "Command Center API" window to stop.
echo.
