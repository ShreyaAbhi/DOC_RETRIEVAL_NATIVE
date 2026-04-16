# ============================================================
#  Document Retrieval System - Launcher
#  Double-click to start all services
# ============================================================

$host.UI.RawUI.WindowTitle = "Doc Retrieval System - Launcher"
$root = $PSScriptRoot
$scripts = "$root\scripts"
$ollamaExe = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"

# Read ports from .env (defaults if not set)
$frontendPort = 5174
$backendPort = 8002
$envFile = "$root\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^FRONTEND_PORT=(\d+)') { $frontendPort = [int]$Matches[1] }
        if ($_ -match '^BACKEND_PORT=(\d+)')  { $backendPort  = [int]$Matches[1] }
    }
}

function Write-Status {
    param([string]$msg, [string]$status, [string]$color = "White")
    $timestamp = Get-Date -Format "HH:mm:ss"
    Write-Host "  [$timestamp] " -NoNewline -ForegroundColor DarkGray
    Write-Host ("{0,-35}" -f $msg) -NoNewline -ForegroundColor White
    Write-Host $status -ForegroundColor $color
}

function Check-Port {
    param([int]$port)
    $conn = Test-NetConnection -ComputerName localhost -Port $port -WarningAction SilentlyContinue -InformationLevel Quiet
    return $conn
}

# ── Update check (non-blocking, best-effort) ─────────────────
$GITHUB_OWNER = "YOUR_GITHUB_USERNAME"
$GITHUB_REPO  = "DOC_RETRIEVAL_NATIVE"
$updateAvailable = $false
$remoteVersion   = ""

$versionFile = Join-Path $root "VERSION"
$localVersion = if (Test-Path $versionFile) { (Get-Content $versionFile -Raw).Trim() } else { "0.0.0" }

try {
    $ghHeaders = @{ "User-Agent" = "DRS-Launcher" }
    if ($env:DRS_UPDATE_TOKEN) { $ghHeaders["Authorization"] = "token $env:DRS_UPDATE_TOKEN" }
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/releases/latest" -Headers $ghHeaders -TimeoutSec 5
    $remoteVersion = ($rel.tag_name -replace '^v', '').Trim()
    if ([System.Version]$remoteVersion -gt [System.Version]$localVersion) {
        $updateAvailable = $true
    }
} catch {
    # Silently ignore -- no internet, repo not set up yet, etc.
}

Clear-Host
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "   DOCUMENT RETRIEVAL SYSTEM - LAUNCHER    " -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan

if ($updateAvailable) {
    Write-Host ""
    Write-Host "   UPDATE AVAILABLE: v$localVersion --> v$remoteVersion" -ForegroundColor Yellow
    Write-Host "   Press [U] after startup or run UPDATE.bat" -ForegroundColor Yellow
} else {
    Write-Host "   Version: v$localVersion" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  Starting services..." -ForegroundColor White
Write-Host ""

# --- Ollama ---
$ollamaRunning = (netstat -ano | findstr "11434") -ne $null
if ($ollamaRunning) {
    Write-Status "Ollama (AI Engine)" "ALREADY RUNNING" "Green"
} else {
    if (Test-Path $ollamaExe) {
        Start-Process $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 4
        $ollamaRunning = (netstat -ano | findstr "11434") -ne $null
        if ($ollamaRunning) {
            Write-Status "Ollama (AI Engine)" "STARTED" "Green"
        } else {
            Write-Status "Ollama (AI Engine)" "FAILED TO START" "Red"
        }
    } else {
        Write-Status "Ollama (AI Engine)" "NOT INSTALLED" "Red"
    }
}

# --- Redis ---
$redisCli = "C:\Program Files\Redis\redis-cli.exe"
if (Test-Path $redisCli) {
    $ping = & $redisCli ping 2>$null
    if ($ping -eq "PONG") {
        Write-Status "Redis (Cache)" "ALREADY RUNNING" "Green"
    } else {
        Start-Service -Name "Redis" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $ping = & $redisCli ping 2>$null
        if ($ping -eq "PONG") {
            Write-Status "Redis (Cache)" "STARTED" "Green"
        } else {
            Write-Status "Redis (Cache)" "FAILED TO START" "Red"
        }
    }
} else {
    Write-Status "Redis (Cache)" "CLI NOT FOUND - SKIPPING" "Yellow"
}

# --- Backend ---
Start-Process "cmd.exe" -ArgumentList "/c `"$scripts\start_backend.bat`"" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 5
$backendUp = Check-Port $backendPort
if ($backendUp) {
    Write-Status "Backend API (port $backendPort)" "RUNNING" "Green"
} else {
    Write-Status "Backend API (port $backendPort)" "STARTING..." "Yellow"
}

# --- Celery Worker ---
Start-Process "cmd.exe" -ArgumentList "/c `"$scripts\start_worker.bat`"" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 2
Write-Status "Celery Worker" "STARTED (background)" "Green"

# --- Celery Beat ---
Start-Process "cmd.exe" -ArgumentList "/c `"$scripts\start_beat.bat`"" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 1
Write-Status "Celery Beat (Scheduler)" "STARTED (background)" "Green"

# --- Frontend ---
Start-Process "cmd.exe" -ArgumentList "/c `"$scripts\start_frontend.bat`"" -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 5
$frontendUp = Check-Port $frontendPort
if ($frontendUp) {
    Write-Status "Frontend UI (port $frontendPort)" "RUNNING" "Green"
} else {
    Write-Status "Frontend UI (port $frontendPort)" "STARTING..." "Yellow"
}

# --- Summary ---
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "   ALL SERVICES LAUNCHED                   " -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "   Frontend  : http://localhost:$frontendPort" -ForegroundColor White
Write-Host "   Backend   : http://localhost:$backendPort" -ForegroundColor White
Write-Host "   API Docs  : http://localhost:$backendPort/docs" -ForegroundColor White
Write-Host ""
Write-Host "   Login     : fasttrack842001@gmail.com" -ForegroundColor DarkGray
Write-Host "   Password  : Welcome01!" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  OPTIONS:" -ForegroundColor White
Write-Host "   [S] Shut down all services and exit" -ForegroundColor Red
Write-Host "   [U] Check for updates" -ForegroundColor Cyan
Write-Host "   [X] Exit this window (services keep running)" -ForegroundColor Yellow
Write-Host ""

# --- Wait for user input ---
while ($true) {
    $key = $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    $char = $key.Character.ToString().ToUpper()

    if ($char -eq "S") {
        Write-Host ""
        Write-Host "  Shutting down all services..." -ForegroundColor Red
        Write-Host ""

        $proc = netstat -ano | findstr ":$backendPort" | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1
        if ($proc) { Stop-Process -Id $proc -Force -ErrorAction SilentlyContinue }
        Write-Status "Backend API" "STOPPED" "Red"

        $proc = netstat -ano | findstr ":$frontendPort" | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1
        if ($proc) { Stop-Process -Id $proc -Force -ErrorAction SilentlyContinue }
        Write-Status "Frontend UI" "STOPPED" "Red"

        Get-Process -Name "celery" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Status "Celery Worker + Beat" "STOPPED" "Red"

        Get-Process -Name "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Write-Status "Ollama" "STOPPED" "Red"

        Write-Host ""
        Write-Host "  All services stopped. Press any key to close." -ForegroundColor DarkGray
        $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
        exit
    }

    if ($char -eq "U") {
        Write-Host ""
        Write-Host "  Launching updater..." -ForegroundColor Cyan
        Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$root\update.ps1`"" -Wait
        Write-Host "  Updater finished. Exiting launcher." -ForegroundColor DarkGray
        Start-Sleep -Seconds 1
        exit
    }

    if ($char -eq "X") {
        Write-Host ""
        Write-Host "  Exiting launcher. Services continue running in background." -ForegroundColor Yellow
        Start-Sleep -Seconds 1
        exit
    }
}
