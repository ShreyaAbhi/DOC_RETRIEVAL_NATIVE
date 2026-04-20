# ============================================================
#  Document Retrieval System - Install as Windows Services
#  Run as Administrator
# ============================================================
#Requires -RunAsAdministrator

$root = (Resolve-Path "$PSScriptRoot\..").Path
$nssmUrl = "https://nssm.cc/release/nssm-2.24.zip"
$nssmDir = "$root\tools\nssm"
$nssm = "$nssmDir\nssm.exe"
$serviceName = "DRS"  # prefix for all services

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "   DOCUMENT RETRIEVAL SYSTEM               " -ForegroundColor Cyan
Write-Host "   Service Installer                       " -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Install path: $root" -ForegroundColor DarkGray
Write-Host ""

# ── Step 1: Download NSSM if not present ─────────────────────
if (-not (Test-Path $nssm)) {
    Write-Host "  Downloading NSSM..." -ForegroundColor Yellow
    $zipPath = "$env:TEMP\nssm.zip"
    $extractPath = "$env:TEMP\nssm_extract"
    try {
        Invoke-WebRequest -Uri $nssmUrl -OutFile $zipPath -UseBasicParsing
        Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
        New-Item -ItemType Directory -Path $nssmDir -Force | Out-Null
        # Find the 64-bit exe inside the extracted folder
        $found = Get-ChildItem -Path $extractPath -Recurse -Filter "nssm.exe" |
                 Where-Object { $_.DirectoryName -like "*win64*" } |
                 Select-Object -First 1
        if (-not $found) {
            $found = Get-ChildItem -Path $extractPath -Recurse -Filter "nssm.exe" | Select-Object -First 1
        }
        Copy-Item $found.FullName -Destination $nssm -Force
        Remove-Item $zipPath -Force -ErrorAction SilentlyContinue
        Remove-Item $extractPath -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  NSSM downloaded successfully." -ForegroundColor Green
    } catch {
        Write-Host "  ERROR: Could not download NSSM. Please download manually from https://nssm.cc" -ForegroundColor Red
        Write-Host "  Place nssm.exe at: $nssm" -ForegroundColor Red
        exit 1
    }
}

Write-Host "  Using NSSM: $nssm" -ForegroundColor DarkGray
Write-Host ""

# ── Read ports from .env ─────────────────────────────────────
$backendPort = 8002
$envFile = "$root\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^BACKEND_PORT=(\d+)') { $backendPort = [int]$Matches[1] }
    }
}

# ── Paths ────────────────────────────────────────────────────
$pythonExe = "$root\backend\venv\Scripts\python.exe"
$celeryExe = "$root\backend\venv\Scripts\celery.exe"

# Ollama may be installed per-user or system-wide - check both locations
$ollamaExe = $null
$ollamaCandidates = @(
    "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
    "$env:ProgramFiles\Ollama\ollama.exe",
    "C:\Program Files\Ollama\ollama.exe"
)
foreach ($candidate in $ollamaCandidates) {
    if (Test-Path $candidate) { $ollamaExe = $candidate; break }
}

if (-not (Test-Path $pythonExe)) {
    Write-Host "  ERROR: Python venv not found at $pythonExe" -ForegroundColor Red
    exit 1
}

# ── Build frontend for production ────────────────────────────
Write-Host "  Building frontend for production..." -ForegroundColor Yellow
$frontendDir = "$root\frontend"
if (Test-Path "$frontendDir\package.json") {
    Push-Location $frontendDir
    $buildOutput = npm run build 2>&1
    $buildExitCode = $LASTEXITCODE
    Pop-Location
    if ($buildExitCode -ne 0 -or -not (Test-Path "$frontendDir\dist\index.html")) {
        Write-Host "  ERROR: Frontend build failed:" -ForegroundColor Red
        Write-Host ($buildOutput | Out-String) -ForegroundColor Red
        exit 1
    }
    Write-Host "  Frontend built successfully." -ForegroundColor Green
} else {
    Write-Host "  ERROR: frontend\package.json not found." -ForegroundColor Red
    exit 1
}

# ── Helper function ──────────────────────────────────────────
function Install-Service {
    param(
        [string]$Name,
        [string]$DisplayName,
        [string]$Exe,
        [string]$Arguments,
        [string]$WorkingDir,
        [hashtable]$Env = @{}
    )

    # Remove existing service if present
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "  Removing existing service: $Name" -ForegroundColor Yellow
        & $nssm stop $Name 2>&1 | Out-Null
        & $nssm remove $Name confirm 2>&1 | Out-Null
        Start-Sleep -Seconds 1
    }

    Write-Host "  Installing: $DisplayName" -ForegroundColor White -NoNewline

    & $nssm install $Name $Exe $Arguments 2>&1 | Out-Null
    & $nssm set $Name DisplayName $DisplayName 2>&1 | Out-Null
    & $nssm set $Name Description "Document Retrieval System component" 2>&1 | Out-Null
    & $nssm set $Name AppDirectory $WorkingDir 2>&1 | Out-Null
    & $nssm set $Name Start SERVICE_AUTO_START 2>&1 | Out-Null
    & $nssm set $Name AppStopMethodSkip 6 2>&1 | Out-Null
    & $nssm set $Name AppStopMethodConsole 5000 2>&1 | Out-Null
    & $nssm set $Name AppStopMethodWindow 5000 2>&1 | Out-Null
    & $nssm set $Name AppStopMethodThreads 5000 2>&1 | Out-Null

    # Log files
    $logDir = "$root\logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $safeName = $Name.Replace("-", "_")
    & $nssm set $Name AppStdout "$logDir\${safeName}_stdout.log" 2>&1 | Out-Null
    & $nssm set $Name AppStderr "$logDir\${safeName}_stderr.log" 2>&1 | Out-Null
    & $nssm set $Name AppRotateFiles 1 2>&1 | Out-Null
    & $nssm set $Name AppRotateBytes 5242880 2>&1 | Out-Null

    # Environment variables
    if ($Env.Count -gt 0) {
        $envString = ($Env.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`n"
        & $nssm set $Name AppEnvironmentExtra $envString 2>&1 | Out-Null
    }

    Write-Host " - OK" -ForegroundColor Green
}

# ── Install services ─────────────────────────────────────────
Write-Host ""
Write-Host "  Installing services..." -ForegroundColor Cyan
Write-Host ""

# 1. Ollama - check if already running before installing as a service
$ollamaInstalled = $false
$ollamaAlreadyRunning = $false
$ollamaProc = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if ($ollamaProc) {
    $ollamaAlreadyRunning = $true
    Write-Host "  Ollama is already running (user process, PID $($ollamaProc.Id))" -ForegroundColor Green
    Write-Host "    Skipping DRS-Ollama service - not needed while Ollama runs via system tray." -ForegroundColor DarkGray
} elseif ($ollamaExe) {
    Install-Service `
        -Name "$serviceName-Ollama" `
        -DisplayName "DRS - Ollama (AI Engine)" `
        -Exe $ollamaExe `
        -Arguments "serve" `
        -WorkingDir $root
    $ollamaInstalled = $true
} else {
    Write-Host "  Skipping Ollama - not found in any standard location" -ForegroundColor Yellow
    Write-Host "    Checked: $($ollamaCandidates -join ', ')" -ForegroundColor DarkGray
}

# 2. Backend (FastAPI + serves frontend)
Install-Service `
    -Name "$serviceName-Backend" `
    -DisplayName "DRS - Backend API" `
    -Exe $pythonExe `
    -Arguments "-m uvicorn app.main:app --host 0.0.0.0 --port $backendPort" `
    -WorkingDir "$root\backend" `
    -Env @{ "TESSERACT_CMD" = "C:\Program Files\Tesseract-OCR\tesseract.exe" }

# Set service dependency: Backend starts after Redis
& $nssm set "$serviceName-Backend" DependOnService "Redis" 2>&1 | Out-Null

# 3. Celery Worker
Install-Service `
    -Name "$serviceName-Worker" `
    -DisplayName "DRS - Celery Worker" `
    -Exe $celeryExe `
    -Arguments "-A app.core.celery_app worker --loglevel=info -Q pod_tasks --pool=solo" `
    -WorkingDir "$root\backend" `
    -Env @{ "TESSERACT_CMD" = "C:\Program Files\Tesseract-OCR\tesseract.exe" }

# Worker depends on Redis and Backend
& $nssm set "$serviceName-Worker" DependOnService "Redis" "$serviceName-Backend" 2>&1 | Out-Null

# 4. Celery Beat
Install-Service `
    -Name "$serviceName-Beat" `
    -DisplayName "DRS - Celery Beat (Scheduler)" `
    -Exe $celeryExe `
    -Arguments "-A app.core.celery_app beat --loglevel=info" `
    -WorkingDir "$root\backend"

# Beat depends on Redis
& $nssm set "$serviceName-Beat" DependOnService "Redis" 2>&1 | Out-Null

# ── Start all services ───────────────────────────────────────
Write-Host ""
Write-Host "  Starting services..." -ForegroundColor Cyan

if ($ollamaAlreadyRunning) {
    Write-Host "  Ollama - Running (user process)" -ForegroundColor Green
}
$services = @("$serviceName-Backend", "$serviceName-Worker", "$serviceName-Beat")
if ($ollamaInstalled) { $services = @("$serviceName-Ollama") + $services }
foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        Start-Service -Name $svc -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $s = Get-Service -Name $svc
        if ($s.Status -eq "Running") { $color = "Green" } else { $color = "Yellow" }
        Write-Host "  $svc - $($s.Status)" -ForegroundColor $color
    }
}

# ── Summary ──────────────────────────────────────────────────
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "   INSTALLATION COMPLETE                   " -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  The system will now run in the background" -ForegroundColor White
Write-Host "  and auto-start when Windows boots." -ForegroundColor White
Write-Host ""
Write-Host "  Access the application at:" -ForegroundColor White
Write-Host "    http://localhost:$backendPort" -ForegroundColor Green
Write-Host ""
Write-Host "  Logs: $root\logs\" -ForegroundColor DarkGray
Write-Host "  Manage: services.msc or 'Get-Service DRS-*'" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Press any key to close." -ForegroundColor DarkGray
$host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
