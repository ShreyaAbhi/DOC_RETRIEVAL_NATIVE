@echo off
powershell.exe -ExecutionPolicy Bypass -Sta -WindowStyle Hidden -File "%~dp0installer.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Setup wizard encountered an error. Trying with elevated privileges...
    powershell.exe -ExecutionPolicy Bypass -Sta -File "%~dp0installer.ps1"
    pause
)
