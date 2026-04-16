@echo off
title Document Retrieval System — Celery Beat
echo Starting Celery beat scheduler...
cd /d "%~dp0..\backend"

call venv\Scripts\activate.bat
celery -A app.core.celery_app beat --loglevel=info
