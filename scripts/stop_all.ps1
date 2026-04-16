# Document Retrieval System — Stop All
Write-Host "Stopping Document Retrieval System services..." -ForegroundColor Yellow

# Kill uvicorn (backend)
Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*Backend*" -or $_.CommandLine -like "*uvicorn*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

# Kill celery workers
Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*Celery*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

# Kill vite dev server
Get-Process -Name "node" -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -like "*Frontend*"
} | Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "Done. Redis service left running (it is a system service)." -ForegroundColor Green
