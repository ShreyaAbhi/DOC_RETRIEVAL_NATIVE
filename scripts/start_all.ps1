# Document Retrieval System - Start All (No Docker)
# Starts: Backend, Celery Worker, Celery Beat, Frontend
# Prerequisites: Redis must already be running as a service

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $scriptDir

Write-Host "Document Retrieval System - Starting all services" -ForegroundColor Cyan
Write-Host ""

# Verify Redis is reachable
$redisExe = "C:\Program Files\Redis\redis-cli.exe"
if (Test-Path $redisExe) {
    $ping = & $redisExe ping 2>$null
    if ($ping -eq "PONG") {
        Write-Host "[OK] Redis is running" -ForegroundColor Green
    } else {
        Write-Host "[WARN] Redis not responding. Starting Redis service..." -ForegroundColor Yellow
        Start-Service -Name "Redis" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
} else {
    Write-Host "[WARN] Redis CLI not found at expected path. Continuing anyway..." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Starting services in separate windows..." -ForegroundColor Cyan
Write-Host ""

# Start Backend
Start-Process "cmd.exe" -ArgumentList "/c `"$scriptDir\start_backend.bat`"" -WorkingDirectory $root -WindowStyle Hidden

Start-Sleep -Seconds 3

# Start Celery Worker
Start-Process "cmd.exe" -ArgumentList "/c `"$scriptDir\start_worker.bat`"" -WorkingDirectory $root -WindowStyle Hidden

# Start Celery Beat
Start-Process "cmd.exe" -ArgumentList "/c `"$scriptDir\start_beat.bat`"" -WorkingDirectory $root -WindowStyle Hidden

Start-Sleep -Seconds 2

# Start Frontend
Start-Process "cmd.exe" -ArgumentList "/c `"$scriptDir\start_frontend.bat`"" -WorkingDirectory $root -WindowStyle Hidden

Write-Host ""
Write-Host "All services started!" -ForegroundColor Green
Write-Host ""
Write-Host "  Frontend:  http://localhost:5174" -ForegroundColor Cyan
Write-Host "  Backend:   http://localhost:8002" -ForegroundColor Cyan
Write-Host "  API docs:  http://localhost:8002/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "Default login: fasttrack842001@gmail.com / Welcome01!" -ForegroundColor Yellow
