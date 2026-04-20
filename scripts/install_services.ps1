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

# ── Step 0: Stop all existing DRS services before making changes ──
$existingServices = Get-Service -Name "DRS-*" -ErrorAction SilentlyContinue
if ($existingServices) {
    Write-Host "  Stopping existing DRS services..." -ForegroundColor Yellow
    foreach ($es in $existingServices) {
        Write-Host "    Stopping $($es.Name)..." -ForegroundColor DarkGray -NoNewline
        Stop-Service -Name $es.Name -Force -ErrorAction SilentlyContinue
        $waited = 0
        while ($waited -lt 10) {
            $es = Get-Service -Name $es.Name -ErrorAction SilentlyContinue
            if ($es.Status -eq "Stopped") { break }
            Start-Sleep -Seconds 2; $waited += 2
        }
        Write-Host " $($es.Status)" -ForegroundColor $(if ($es.Status -eq "Stopped") { "Green" } else { "Yellow" })
    }
    # Wait for ports to be released
    Start-Sleep -Seconds 3
    Write-Host "  All existing services stopped." -ForegroundColor Green
    Write-Host ""
}

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
    # Always restart on crash with a 15-second delay (allows port release)
    & $nssm set $Name AppExit Default Restart 2>&1 | Out-Null
    & $nssm set $Name AppRestartDelay 15000 2>&1 | Out-Null

    # Log files
    $logDir = "$root\logs"
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    $safeName = $Name.Replace("-", "_")
    & $nssm set $Name AppStdout "$logDir\${safeName}_stdout.log" 2>&1 | Out-Null
    & $nssm set $Name AppStderr "$logDir\${safeName}_stderr.log" 2>&1 | Out-Null
    & $nssm set $Name AppRotateFiles 1 2>&1 | Out-Null
    & $nssm set $Name AppRotateBytes 5242880 2>&1 | Out-Null

    # Environment variables — pass each as a separate argument to NSSM
    if ($Env.Count -gt 0) {
        $envArgs = @($Name, "AppEnvironmentExtra")
        $Env.GetEnumerator() | ForEach-Object { $envArgs += "$($_.Key)=$($_.Value)" }
        & $nssm set @envArgs 2>&1 | Out-Null
    }

    Write-Host " - OK" -ForegroundColor Green
}

# ── Install services ─────────────────────────────────────────
Write-Host ""
Write-Host "  Installing services..." -ForegroundColor Cyan
Write-Host ""

# 1. Ollama - stop any user-mode instance and install as a proper service
$ollamaInstalled = $false

# Stop any existing DRS-Ollama service first to free port 11434
$existingOllamaSvc = Get-Service -Name "$serviceName-Ollama" -ErrorAction SilentlyContinue
if ($existingOllamaSvc) {
    Write-Host "  Stopping existing DRS-Ollama service..." -ForegroundColor Yellow
    & $nssm stop "$serviceName-Ollama" 2>&1 | Out-Null
    Start-Sleep -Seconds 3
}

# Stop Ollama tray app / user processes so we can run it as a service
$ollamaProc = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
if ($ollamaProc) {
    Write-Host "  Stopping Ollama tray app / user processes..." -ForegroundColor Yellow
    $ollamaProc | ForEach-Object {
        Write-Host "    Stopping $($_.ProcessName) (PID $($_.Id))..." -ForegroundColor DarkGray
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 3
    # Verify all stopped
    $remaining = Get-Process -Name "ollama*" -ErrorAction SilentlyContinue
    if ($remaining) {
        Write-Host "  WARNING: Some Ollama processes could not be stopped." -ForegroundColor Red
        Write-Host "    Please close Ollama from the system tray and re-run this installer." -ForegroundColor Red
    } else {
        Write-Host "  Ollama user processes stopped." -ForegroundColor Green
    }
}

# Wait for port 11434 to be free
$portCheck = Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
if ($portCheck) {
    Write-Host "  Waiting for port 11434 to be released..." -ForegroundColor Yellow
    $waited = 0
    while ($waited -lt 15) {
        Start-Sleep -Seconds 2
        $waited += 2
        $portCheck = Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
        if (-not $portCheck) { break }
    }
    if ($portCheck) {
        Write-Host "  WARNING: Port 11434 still in use. Ollama service may fail to start." -ForegroundColor Red
    }
}

if ($ollamaExe) {
    # Copy Ollama models to a system-accessible location (SYSTEM account cannot access user profile)
    $systemModelsDir = "C:\ProgramData\Ollama\models"
    $userModelsDir = $null
    $modelsCandidates = @(
        "$env:USERPROFILE\.ollama\models",
        "$env:LOCALAPPDATA\Ollama\models",
        "C:\Users\$env:USERNAME\.ollama\models"
    )
    foreach ($mc in $modelsCandidates) {
        if (Test-Path $mc) { $userModelsDir = $mc; break }
    }

    if ($userModelsDir -and -not (Test-Path "$systemModelsDir\manifests")) {
        Write-Host "  Copying Ollama models to system location..." -ForegroundColor Yellow
        Write-Host "    From: $userModelsDir" -ForegroundColor DarkGray
        Write-Host "    To:   $systemModelsDir" -ForegroundColor DarkGray
        New-Item -ItemType Directory -Path $systemModelsDir -Force | Out-Null
        Copy-Item -Path "$userModelsDir\*" -Destination $systemModelsDir -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "  Models copied." -ForegroundColor Green
    } elseif (Test-Path "$systemModelsDir\manifests") {
        Write-Host "  Using existing system Ollama models: $systemModelsDir" -ForegroundColor DarkGray
    } elseif ($userModelsDir) {
        Write-Host "  Using user Ollama models: $userModelsDir" -ForegroundColor DarkGray
        $systemModelsDir = $userModelsDir
    }

    $ollamaEnv = @{ "OLLAMA_HOST" = "0.0.0.0:11434"; "OLLAMA_MODELS" = $systemModelsDir }

    Install-Service `
        -Name "$serviceName-Ollama" `
        -DisplayName "DRS - Ollama (AI Engine)" `
        -Exe $ollamaExe `
        -Arguments "serve" `
        -WorkingDir $root `
        -Env $ollamaEnv
    $ollamaInstalled = $true

    # Disable Ollama tray app auto-start to prevent conflicts with the service
    $startupReg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $ollamaStartup = Get-ItemProperty -Path $startupReg -Name "Ollama" -ErrorAction SilentlyContinue
    if ($ollamaStartup) {
        Remove-ItemProperty -Path $startupReg -Name "Ollama" -ErrorAction SilentlyContinue
        Write-Host "  Disabled Ollama tray app auto-start (now runs as service instead)" -ForegroundColor DarkGray
    }
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

# Check if Redis is installed as a Windows service and ensure it is running
$redisService = Get-Service -Name "Redis" -ErrorAction SilentlyContinue
if ($redisService) {
    if ($redisService.Status -ne "Running") {
        Write-Host "  Starting Redis service..." -ForegroundColor Yellow
        Start-Service -Name "Redis" -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
        $redisService = Get-Service -Name "Redis" -ErrorAction SilentlyContinue
    }
    if ($redisService.Status -eq "Running") {
        Write-Host "  Redis service is running" -ForegroundColor Green
    } else {
        Write-Host "  WARNING: Redis service exists but could not be started" -ForegroundColor Red
    }
    & $nssm set "$serviceName-Backend" DependOnService "Redis" 2>&1 | Out-Null
} else {
    Write-Host "  WARNING: Redis service not found!" -ForegroundColor Red
    Write-Host "    Celery Worker and Beat require Redis. Install with: winget install Redis.Redis" -ForegroundColor Yellow
}

# 3. Celery Worker
Install-Service `
    -Name "$serviceName-Worker" `
    -DisplayName "DRS - Celery Worker" `
    -Exe $celeryExe `
    -Arguments "-A app.core.celery_app worker --loglevel=info -Q pod_tasks --pool=solo" `
    -WorkingDir "$root\backend" `
    -Env @{ "TESSERACT_CMD" = "C:\Program Files\Tesseract-OCR\tesseract.exe" }

# Worker depends on Backend (and Redis if available)
if ($redisService) {
    & $nssm set "$serviceName-Worker" DependOnService "Redis" "$serviceName-Backend" 2>&1 | Out-Null
} else {
    & $nssm set "$serviceName-Worker" DependOnService "$serviceName-Backend" 2>&1 | Out-Null
}

# 4. Celery Beat (schedule file stored in logs dir for write access)
Install-Service `
    -Name "$serviceName-Beat" `
    -DisplayName "DRS - Celery Beat (Scheduler)" `
    -Exe $celeryExe `
    -Arguments "-A app.core.celery_app beat --loglevel=info --schedule=$root\logs\celerybeat-schedule" `
    -WorkingDir "$root\backend"

# Beat depends on Redis if available
if ($redisService) {
    & $nssm set "$serviceName-Beat" DependOnService "Redis" 2>&1 | Out-Null
}

# ── Start all services ───────────────────────────────────────
Write-Host ""
Write-Host "  Starting services..." -ForegroundColor Cyan

$services = @("$serviceName-Backend", "$serviceName-Worker", "$serviceName-Beat")
if ($ollamaInstalled) { $services = @("$serviceName-Ollama") + $services }
$allRunning = $true
$failedServices = @{}
foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if (-not $s) {
        Write-Host "  $svc - NOT FOUND (install may have failed)" -ForegroundColor Red
        $allRunning = $false
        $failedServices[$svc] = "Service not found after installation"
        continue
    }

    $started = $false
    $lastError = ""
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        if ($attempt -gt 1) {
            Write-Host "  $svc - retry $attempt of 3..." -ForegroundColor Yellow
        }
        Start-Service -Name $svc -ErrorAction SilentlyContinue 2>&1 | Out-Null
        # Wait up to 15 seconds for the service to reach Running state
        $waited = 0
        while ($waited -lt 15) {
            Start-Sleep -Seconds 2
            $waited += 2
            $s = Get-Service -Name $svc
            if ($s.Status -eq "Running") { break }
        }
        if ($s.Status -eq "Running") {
            $started = $true
            break
        }

        # Capture reason for failure before retrying
        $lastError = "Service status: $($s.Status)"

        # Check Windows Event Log for the most recent error from this service
        $evt = Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'; Level=2; StartTime=(Get-Date).AddMinutes(-2)} -MaxEvents 5 -ErrorAction SilentlyContinue |
               Where-Object { $_.Message -like "*$svc*" } | Select-Object -First 1
        if ($evt) { $lastError += "`n    Event Log: $($evt.Message.Trim())" }

        # Check NSSM's recorded exit code
        $exitCode = & $nssm get $svc AppExitCode 2>&1
        if ($exitCode -and $exitCode -notmatch "No exit code") {
            $lastError += "`n    Last exit code: $exitCode"
        }

        # Read last 10 lines of stderr log for the actual error
        $safeName = $svc.Replace("-", "_")
        $stderrLog = "$root\logs\${safeName}_stderr.log"
        if (Test-Path $stderrLog) {
            $tail = Get-Content $stderrLog -Tail 10 -ErrorAction SilentlyContinue
            if ($tail) { $lastError += "`n    Recent stderr:`n      " + ($tail -join "`n      ") }
        }

        Write-Host "    Attempt $attempt failed - $($s.Status)" -ForegroundColor DarkGray

        # Stop before retrying
        & $nssm stop $svc 2>&1 | Out-Null
        Start-Sleep -Seconds 3
    }

    if ($started) {
        Write-Host "  $svc - Running" -ForegroundColor Green
    } else {
        Write-Host "  $svc - FAILED after 3 attempts ($($s.Status))" -ForegroundColor Red
        $allRunning = $false
        $failedServices[$svc] = $lastError
    }
}

if (-not $allRunning) {
    Write-Host ""
    Write-Host "  ==========================================" -ForegroundColor Red
    Write-Host "   SOME SERVICES FAILED TO START            " -ForegroundColor Red
    Write-Host "  ==========================================" -ForegroundColor Red
    Write-Host ""

    # Write diagnostic report to file and console
    $diagFile = "$root\logs\install_diagnostic.log"
    $diagLines = @()
    $diagLines += "=== DRS Service Install Diagnostic Report ==="
    $diagLines += "Date: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    $diagLines += "Install path: $root"
    $diagLines += ""

    foreach ($fSvc in $failedServices.Keys) {
        $info = $failedServices[$fSvc]
        Write-Host "  --- $fSvc ---" -ForegroundColor Yellow
        $diagLines += "--- $fSvc ---"

        # Show the captured error info
        $info -split "`n" | ForEach-Object {
            Write-Host "  $_" -ForegroundColor Gray
            $diagLines += "  $_"
        }

        # Show NSSM configuration for this service
        $diagLines += "  NSSM config:"
        $appPath = & $nssm get $fSvc Application 2>&1
        $appArgs = & $nssm get $fSvc AppParameters 2>&1
        $appDir  = & $nssm get $fSvc AppDirectory 2>&1
        $appEnv  = & $nssm get $fSvc AppEnvironmentExtra 2>&1
        $deps    = & $nssm get $fSvc DependOnService 2>&1
        $diagLines += "    Application: $appPath"
        $diagLines += "    Arguments:   $appArgs"
        $diagLines += "    WorkingDir:  $appDir"
        $diagLines += "    EnvExtra:    $appEnv"
        $diagLines += "    DependsOn:   $deps"

        # Check if the executable actually exists
        if ($appPath -and -not (Test-Path $appPath)) {
            $msg = "  PROBLEM: Executable not found at $appPath"
            Write-Host "  $msg" -ForegroundColor Red
            $diagLines += "  $msg"
        }

        # Check dependencies are running
        if ($deps -and $deps -notmatch "returned") {
            $depList = $deps -split "`n" | Where-Object { $_.Trim() }
            foreach ($dep in $depList) {
                $depSvc = Get-Service -Name $dep.Trim() -ErrorAction SilentlyContinue
                if (-not $depSvc) {
                    $msg = "  PROBLEM: Dependency '$dep' does not exist"
                    Write-Host "  $msg" -ForegroundColor Red
                    $diagLines += "  $msg"
                } elseif ($depSvc.Status -ne "Running") {
                    $msg = "  PROBLEM: Dependency '$dep' is $($depSvc.Status), not Running"
                    Write-Host "  $msg" -ForegroundColor Red
                    $diagLines += "  $msg"
                }
            }
        }

        # Port conflict check for known ports
        if ($fSvc -like "*Ollama*") {
            $portInUse = Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
            if ($portInUse) {
                $pid = $portInUse[0].OwningProcess
                $procName = (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName
                $msg = "  PROBLEM: Port 11434 held by $procName (PID $pid)"
                Write-Host "  $msg" -ForegroundColor Red
                $diagLines += "  $msg"
            }
        }
        if ($fSvc -like "*Backend*") {
            $portInUse = Get-NetTCPConnection -LocalPort $backendPort -ErrorAction SilentlyContinue
            if ($portInUse) {
                $pid = $portInUse[0].OwningProcess
                $procName = (Get-Process -Id $pid -ErrorAction SilentlyContinue).ProcessName
                $msg = "  PROBLEM: Port $backendPort held by $procName (PID $pid)"
                Write-Host "  $msg" -ForegroundColor Red
                $diagLines += "  $msg"
            }
        }

        Write-Host ""
        $diagLines += ""
    }

    # Save diagnostic report to file
    $diagLines | Out-File -FilePath $diagFile -Encoding UTF8 -Force
    Write-Host "  Full diagnostic saved to:" -ForegroundColor Yellow
    Write-Host "    $diagFile" -ForegroundColor White
    Write-Host ""
    Write-Host "  Service logs at: $root\logs\" -ForegroundColor Yellow
    Write-Host "  Then re-run this installer to try again." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Press any key to close." -ForegroundColor DarkGray
    $host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
    exit 1
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
