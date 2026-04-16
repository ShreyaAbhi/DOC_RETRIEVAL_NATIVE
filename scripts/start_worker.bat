@echo off
title Document Retrieval System — Celery Worker
echo Starting Celery worker...
cd /d "%~dp0..\backend"

set TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe

call venv\Scripts\activate.bat
celery -A app.core.celery_app worker --loglevel=info -Q pod_tasks --pool=solo
