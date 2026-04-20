@echo off
title Document Retrieval System — Uninstall Services
echo.
echo   Launching service uninstaller (requires Administrator)...
echo.

:: Re-launch elevated if not already admin
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo   Requesting administrator privileges...
    powershell -Command "Start-Process cmd -ArgumentList '/c \"%~f0\"' -Verb RunAs"
    exit /b
)

:: Run the PowerShell uninstaller with bypass policy
powershell -ExecutionPolicy Bypass -File "%~dp0uninstall_services.ps1"

:: If PowerShell fails, keep window open so user can see the error
if %errorlevel% neq 0 (
    echo.
    echo   Something went wrong. See the error above.
    echo   Press any key to close.
    pause >nul
)
