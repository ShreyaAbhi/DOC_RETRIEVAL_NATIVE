@echo off
title Document Retrieval System - Backend
echo Starting backend...
cd /d "%~dp0.."

set BACKEND_PORT=8002
for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
    if "%%a"=="BACKEND_PORT" set BACKEND_PORT=%%b
)

cd backend
set TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
call venv\Scripts\activate.bat
uvicorn app.main:app --host 0.0.0.0 --port %BACKEND_PORT% --reload
