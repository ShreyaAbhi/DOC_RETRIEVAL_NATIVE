#Requires -Version 5.1
<#
.SYNOPSIS
    Document Retrieval System - Setup Wizard
.DESCRIPTION
    Guides you step-by-step through installing all prerequisites and starting
    Document Retrieval System (no Docker required).
.NOTES
    Run with: Install.bat  (double-click)
    Or:       powershell -ExecutionPolicy Bypass -Sta -File installer.ps1
#>

# Ensure STA apartment state for WinForms
if ([System.Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') {
    Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -Sta -WindowStyle Hidden -File `"$PSCommandPath`"" -Wait
    exit
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# DPI awareness - prevents Windows from blurring/scaling the window
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class DpiHelper {
    [DllImport("user32.dll")]
    public static extern bool SetProcessDPIAware();
    [DllImport("shcore.dll")]
    public static extern int SetProcessDpiAwareness(int awareness);
}
"@
try { [DpiHelper]::SetProcessDpiAwareness(2) } catch {}
try { [DpiHelper]::SetProcessDPIAware() }       catch {}

[System.Windows.Forms.Application]::EnableVisualStyles()
[System.Windows.Forms.Application]::SetCompatibleTextRenderingDefault($false)

# $script: prefix so closures and event handlers always find this variable
$script:INSTALL_DIR = $PSScriptRoot

# ---- Helpers ------------------------------------------------------------------

function Invoke-Check($block) {
    try { return (& $block) -eq $true } catch { return $false }
}

function Run-Action($block) {
    try { & $block; return $true } catch { return $false }
}

function Find-Python {
    $p = Get-Command python  -ErrorAction SilentlyContinue
    if ($p) { return $p.Source }
    $p = Get-Command python3 -ErrorAction SilentlyContinue
    if ($p) { return $p.Source }
    return $null
}

# ---- Step definitions ---------------------------------------------------------

# $script: prefix required - referenced from event handlers and closures
$script:steps = @(

    @{
        Icon        = "1"
        Title       = "System Prerequisites"
        Desc        = "Document Retrieval System requires Python 3.12+, Node.js 20+, Redis, LibreOffice, and Tesseract OCR. Click Install Prerequisites to automatically install any that are missing via winget. SQLite is built into Python - no separate database server is needed."
        LinkUrl     = $null
        LinkText    = $null
        Note        = "Installation requires an internet connection and may take several minutes. A command window will open while components install. If prompted to accept licence terms, press Y to continue."
        ActionLabel = "Install Missing Prerequisites"
        Action      = {
            # Fall back to LOCALAPPDATA if TEMP is not set
            $log = if ($env:TEMP) { Join-Path $env:TEMP "drs_prereqs.log" } `
                   else           { Join-Path $env:LOCALAPPDATA "drs_prereqs.log" }

            # Use single & (unconditional) not && so every winget runs even when
            # a previous one exits non-zero (winget exits non-zero for already-installed packages)
            $cmds = @(
                "echo Installing prerequisites... > `"$log`""
                "winget install --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements >> `"$log`" 2>&1"
                "winget install --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements >> `"$log`" 2>&1"
                "winget install --id Redis.Redis --accept-source-agreements --accept-package-agreements >> `"$log`" 2>&1"
                "winget install --id TheDocumentFoundation.LibreOffice --accept-source-agreements --accept-package-agreements >> `"$log`" 2>&1"
                "winget install --id UB-Mannheim.TesseractOCR --accept-source-agreements --accept-package-agreements >> `"$log`" 2>&1"
                "net start Redis >> `"$log`" 2>&1 || net start redis >> `"$log`" 2>&1 || echo Redis service start attempted >> `"$log`" 2>&1"
                "echo."
                "echo All done.  Log: `"$log`""
                "pause"
            )
            $cmdStr = $cmds -join " & "
            Start-Process cmd -ArgumentList "/c $cmdStr" -Wait -WindowStyle Normal
        }
        Check       = {
            $script:prereqMissing = @()

            # Refresh PATH from registry so newly-installed tools are visible
            # without needing to restart the installer
            try {
                $machinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
                $userPath    = [System.Environment]::GetEnvironmentVariable("Path", "User")
                $env:Path    = (@($machinePath, $userPath) | Where-Object { $_ }) -join ";"
            } catch {}

            # Python 3.12+
            $pyOk = $false
            try {
                $ver = & python --version 2>&1
                if ($ver -match "Python (\d+)\.(\d+)") {
                    $major = [int]$Matches[1]; $minor = [int]$Matches[2]
                    $pyOk  = ($major -gt 3) -or ($major -eq 3 -and $minor -ge 12)
                }
            } catch {}
            if (-not $pyOk) { $script:prereqMissing += "Python 3.12+" }

            # Node.js
            $nodeOk = $false
            try { $nodeOk = ($null -ne (Get-Command node -ErrorAction SilentlyContinue)) } catch {}
            if (-not $nodeOk) { $script:prereqMissing += "Node.js" }

            # Redis - check multiple service names (Redis, Memurai), process, and port 6379
            $redisOk = $false
            try {
                $svc = Get-Service -Name @("Redis","Memurai","redis") -ErrorAction SilentlyContinue |
                       Where-Object { $_.Status -eq "Running" } |
                       Select-Object -First 1
                if ($svc) { $redisOk = $true }

                if (-not $redisOk) {
                    $proc = Get-Process -Name @("redis-server","memurai") -ErrorAction SilentlyContinue |
                            Select-Object -First 1
                    if ($proc) { $redisOk = $true }
                }
                if (-not $redisOk) {
                    $conn = Test-NetConnection -ComputerName localhost -Port 6379 `
                                -ErrorAction SilentlyContinue -WarningAction SilentlyContinue
                    if ($conn -and $conn.TcpTestSucceeded) { $redisOk = $true }
                }
            } catch {}
            if (-not $redisOk) { $script:prereqMissing += "Redis (not installed or not running)" }

            # LibreOffice - check both 64-bit and 32-bit install paths
            $loOk = (Test-Path "C:\Program Files\LibreOffice\program\soffice.exe") -or
                    (Test-Path "C:\Program Files (x86)\LibreOffice\program\soffice.exe")
            if (-not $loOk) { $script:prereqMissing += "LibreOffice" }

            # Tesseract - check both 64-bit and 32-bit install paths
            $tessOk = (Test-Path "C:\Program Files\Tesseract-OCR\tesseract.exe") -or
                      (Test-Path "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe")
            if (-not $tessOk) { $script:prereqMissing += "Tesseract OCR" }

            return ($script:prereqMissing.Count -eq 0)
        }
        Version     = {
            try { (& python --version 2>&1) + " | Node " + (& node --version 2>&1) } catch { "" }
        }
        OkMsg       = "All prerequisites are installed and running."
        ErrMsg      = {
            $missing = if ($script:prereqMissing.Count -gt 0) {
                ($script:prereqMissing | ForEach-Object { " * $_" }) -join "`n"
            } else { " * (run Check Again to identify)" }
            "Missing or not running:`n$missing`n`n * Click Install Missing Prerequisites above`n * If just installed, close and reopen this installer (PATH refresh needed)`n * For Redis specifically: open Services (services.msc) and start the Redis service manually"
        }
    },

    @{
        Icon        = "2"
        Title       = "Install Ollama"
        Desc        = "Ollama runs AI language models locally on your machine. Document Retrieval System uses it to classify incoming emails and compose professional replies."
        LinkUrl     = "https://ollama.com/download/windows"
        LinkText    = "Download Ollama for Windows"
        Note        = "After installing Ollama, click Pull AI Model below to download the required model (approx. 2 GB). This is a one-time download and requires an internet connection."
        ActionLabel = "Pull AI Model  (qwen2.5:3b)"
        Action      = {
            Start-Process cmd -ArgumentList "/c ollama pull qwen2.5:3b && echo. && echo Model download complete! && pause" `
                -Wait -WindowStyle Normal
        }
        Check       = {
            # Query the Ollama REST API directly with a timeout.
            # Calling `ollama list` via & hangs the UI thread when the daemon is
            # not yet running because the CLI tries to auto-start the server and
            # waits indefinitely instead of throwing a catchable exception.
            try {
                $r = Invoke-WebRequest -Uri "http://localhost:11434/api/tags" `
                         -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
                return ($r.Content | Select-String "qwen2.5" | Measure-Object).Count -gt 0
            } catch { return $false }
        }
        Version     = { try { (& ollama --version 2>&1) } catch { "" } }
        OkMsg       = "Ollama is installed and the qwen2.5:3b model is ready."
        ErrMsg      = "Ollama or the AI model was not found.

 * Download and install Ollama using the button above
 * After installing, click Pull AI Model to download qwen2.5:3b
 * The download is approx. 2 GB - make sure you have an internet connection
 * Then click Check Again"
    },

    @{
        Icon        = "3"
        Title       = "Application Setup"
        Desc        = "Installs Python packages and frontend dependencies. This step downloads and installs all application libraries - it only needs to be done once and requires an internet connection."
        LinkUrl     = $null
        LinkText    = $null
        Note        = "Two windows will open: one for Python packages (~50 packages) and one for frontend packages. This may take 3-5 minutes on first run. Do not close the windows until they complete."
        ActionLabel = "Install Application Dependencies"
        Action      = {
            # $script:INSTALL_DIR - plain $INSTALL_DIR is not in scope inside this block
            $backendDir  = Join-Path $script:INSTALL_DIR "backend"
            $frontendDir = Join-Path $script:INSTALL_DIR "frontend"

            # ── Sanitise .env: ensure DATABASE_URL points to SQLite, not PostgreSQL ──
            # An old PostgreSQL installation may have left a .env file with
            # DATABASE_URL=postgresql://...  Pydantic-settings reads this file and
            # would override the SQLite default, causing the backend to fail.
            $envFile   = Join-Path $backendDir ".env"
            $sqliteUrl = "sqlite+aiosqlite:///./pod_system.db"
            if (Test-Path $envFile) {
                $lines    = Get-Content $envFile
                $dbLine   = $lines | Where-Object { $_ -match "^DATABASE_URL\s*=" }
                if ($dbLine) {
                    # Replace whatever DATABASE_URL was set to with the SQLite URL
                    $lines = $lines | ForEach-Object {
                        if ($_ -match "^DATABASE_URL\s*=") { "DATABASE_URL=$sqliteUrl" } else { $_ }
                    }
                    $lines | Set-Content $envFile
                }
                # If there is no DATABASE_URL line at all, the config.py default is used (SQLite) — no change needed
            } else {
                # Create a minimal .env that pins the SQLite URL
                "DATABASE_URL=$sqliteUrl" | Set-Content $envFile
            }

            # Python: sequential with && (each step depends on the previous succeeding)
            $pyCmds = @(
                "cd /d `"$backendDir`""
                "echo Creating Python virtual environment..."
                "python -m venv venv"
                "echo Installing Python packages..."
                "venv\Scripts\pip install -r requirements.txt"
                "echo."
                "echo Python setup complete!"
                "pause"
            )
            $pyStr  = $pyCmds -join " && "
            $pyProc = Start-Process cmd -ArgumentList "/c $pyStr" -PassThru -WindowStyle Normal
            $pyProc.WaitForExit()

            # Frontend: sequential with &&
            $npmCmds = @(
                "cd /d `"$frontendDir`""
                "echo Installing frontend packages..."
                "npm install"
                "echo."
                "echo Frontend setup complete!"
                "pause"
            )
            $npmStr  = $npmCmds -join " && "
            $npmProc = Start-Process cmd -ArgumentList "/c $npmStr" -PassThru -WindowStyle Normal
            $npmProc.WaitForExit()
        }
        Check       = {
            $venvPy      = Join-Path $script:INSTALL_DIR "backend\venv\Scripts\python.exe"
            $nodeModules = Join-Path $script:INSTALL_DIR "frontend\node_modules"
            $fastapi     = Join-Path $script:INSTALL_DIR "backend\venv\Lib\site-packages\fastapi"
            return (Test-Path $venvPy) -and (Test-Path $nodeModules) -and (Test-Path $fastapi)
        }
        Version     = { "" }
        OkMsg       = "Python environment and frontend packages are installed."
        ErrMsg      = "Application dependencies are not fully installed.

 * Click Install Application Dependencies above
 * Make sure you have an internet connection
 * If Python is not found, complete Step 1 first
 * Then click Check Again"
    },

    @{
        Icon        = "4"
        Title       = "Start and Verify"
        Desc        = "Everything is ready. Click Start System to launch Document Retrieval System. Four windows will open: the API backend, two background workers, and the web interface."
        LinkUrl     = "http://localhost:5174"
        LinkText    = "Open Document Retrieval System"
        Note        = "Default login:
  Email:     fasttrack842001@gmail.com
  Password:  Welcome01!

Starts automatically via start_all.ps1 in the scripts folder.
Redis starts with Windows. The SQLite database is created automatically on first launch.

PORT CONFIGURATION: If ports 5174 (frontend) or 8002 (backend) conflict with other
software on this machine, edit the .env file in the install folder and change
FRONTEND_PORT and BACKEND_PORT to any available port numbers. All services read
these values automatically on next start."
        ActionLabel = "Start System"
        Action      = {
            $startScript = Join-Path $script:INSTALL_DIR "scripts\start_all.ps1"
            Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -File `"$startScript`"" -WindowStyle Normal
            Start-Sleep -Seconds 18
        }
        Check       = {
            try {
                $r = Invoke-WebRequest -Uri "http://localhost:5174" -UseBasicParsing `
                         -TimeoutSec 8 -ErrorAction Stop
                return $r.StatusCode -lt 400
            } catch { return $false }
        }
        Version     = { "" }
        OkMsg       = "Document Retrieval System is running at http://localhost:5174"
        ErrMsg      = "The system is not responding at http://localhost:5174.

 * Click Start System above to launch all services
 * Wait 20 seconds for all services to fully start, then click Check Again
 * Make sure Redis service is running (check Windows Services)"
    }
)

# ---- Colours ------------------------------------------------------------------

$C_DARK    = [System.Drawing.Color]::FromArgb(15,  23,  42)
$C_DARK2   = [System.Drawing.Color]::FromArgb(30,  41,  59)
$C_BG      = [System.Drawing.Color]::FromArgb(248, 250, 252)
$C_WHITE   = [System.Drawing.Color]::White
$C_BLUE    = [System.Drawing.Color]::FromArgb(59,  130, 246)
$C_GREEN   = [System.Drawing.Color]::FromArgb(22,  163, 74)
$C_GREEN_B = [System.Drawing.Color]::FromArgb(240, 253, 244)
$C_RED     = [System.Drawing.Color]::FromArgb(220, 38,  38)
$C_RED_B   = [System.Drawing.Color]::FromArgb(254, 242, 242)
$C_MUTED   = [System.Drawing.Color]::FromArgb(100, 116, 139)
$C_BORDER  = [System.Drawing.Color]::FromArgb(226, 232, 240)
$C_AMBER   = [System.Drawing.Color]::FromArgb(180, 83,  9)
$C_AMBER_B = [System.Drawing.Color]::FromArgb(255, 251, 235)

# ---- Fonts --------------------------------------------------------------------

$F_BODY  = New-Object System.Drawing.Font("Segoe UI", 9)
$F_SM    = New-Object System.Drawing.Font("Segoe UI", 8.5)
$F_TITLE = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
$F_HDR   = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)
$F_BADGE = New-Object System.Drawing.Font("Segoe UI", 11, [System.Drawing.FontStyle]::Bold)

# ---- Form ---------------------------------------------------------------------

$form = New-Object System.Windows.Forms.Form
$form.Text            = "Document Retrieval System - Setup Wizard"
$form.ClientSize      = New-Object System.Drawing.Size(700, 610)
$form.StartPosition   = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox     = $false
$form.BackColor       = $C_BG
$form.Font            = $F_BODY
$form.AutoScaleMode   = [System.Windows.Forms.AutoScaleMode]::None

# ---- Header panel -------------------------------------------------------------

$pnlHeader = New-Object System.Windows.Forms.Panel
$pnlHeader.Dock      = "Top"
$pnlHeader.Height    = 72
$pnlHeader.BackColor = $C_DARK

$lblAppTitle = New-Object System.Windows.Forms.Label
$lblAppTitle.Text      = "Document Retrieval System - Setup Wizard"
$lblAppTitle.ForeColor = $C_WHITE
$lblAppTitle.Font      = $F_HDR
$lblAppTitle.AutoSize  = $false
$lblAppTitle.Location  = New-Object System.Drawing.Point(20, 14)
$lblAppTitle.Size      = New-Object System.Drawing.Size(480, 24)

$lblStepInfo = New-Object System.Windows.Forms.Label
$lblStepInfo.ForeColor = [System.Drawing.Color]::FromArgb(148, 163, 184)
$lblStepInfo.Font      = $F_SM
$lblStepInfo.AutoSize  = $false
$lblStepInfo.Location  = New-Object System.Drawing.Point(20, 42)
$lblStepInfo.Size      = New-Object System.Drawing.Size(480, 18)

$pnlHeader.Controls.AddRange(@($lblAppTitle, $lblStepInfo))

# ---- Progress strip -----------------------------------------------------------

$pnlProgress = New-Object System.Windows.Forms.Panel
$pnlProgress.Dock      = "Top"
$pnlProgress.Height    = 6
$pnlProgress.BackColor = $C_DARK2

$pnlProgressFill = New-Object System.Windows.Forms.Panel
$pnlProgressFill.Location  = New-Object System.Drawing.Point(0, 0)
$pnlProgressFill.Height    = 6
$pnlProgressFill.BackColor = $C_BLUE
$pnlProgress.Controls.Add($pnlProgressFill)

# ---- Content area -------------------------------------------------------------
#
# Layout has two modes based on whether the current step has a LinkUrl:
#
#   WITH link button:                   WITHOUT link button:
#   Y=24   badge + title                Y=24   badge + title
#   Y=86   description  (h=52)          Y=86   description  (h=52)
#   Y=148  link button  (h=36)          Y=148  separator
#   Y=198  separator                    Y=158  note         (h=90, bottom=248)
#   Y=208  note         (h=90, b=298)   Y=262  action btn   (h=38, bottom=300)
#   Y=310  action btn   (h=38, b=348)
#
# All elements fit inside the 370 px content panel height.

$pnlContent = New-Object System.Windows.Forms.Panel
$pnlContent.Location  = New-Object System.Drawing.Point(0, 78)
$pnlContent.Size      = New-Object System.Drawing.Size(700, 370)
$pnlContent.BackColor = $C_BG

$lblBadge = New-Object System.Windows.Forms.Label
$lblBadge.Size      = New-Object System.Drawing.Size(48, 48)
$lblBadge.Location  = New-Object System.Drawing.Point(28, 24)
$lblBadge.TextAlign = "MiddleCenter"
$lblBadge.Font      = $F_BADGE
$lblBadge.ForeColor = $C_WHITE
$lblBadge.BackColor = $C_BLUE

$lblStepTitle = New-Object System.Windows.Forms.Label
$lblStepTitle.Location  = New-Object System.Drawing.Point(90, 24)
$lblStepTitle.Size      = New-Object System.Drawing.Size(580, 44)
$lblStepTitle.Font      = $F_TITLE
$lblStepTitle.ForeColor = $C_DARK
$lblStepTitle.AutoSize  = $false

$lblDesc = New-Object System.Windows.Forms.Label
$lblDesc.Location  = New-Object System.Drawing.Point(28, 86)
$lblDesc.Size      = New-Object System.Drawing.Size(644, 52)
$lblDesc.Font      = $F_BODY
$lblDesc.ForeColor = $C_MUTED
$lblDesc.AutoSize  = $false

$btnLink = New-Object System.Windows.Forms.Button
$btnLink.Location    = New-Object System.Drawing.Point(28, 148)
$btnLink.Size        = New-Object System.Drawing.Size(320, 36)
$btnLink.Font        = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$btnLink.FlatStyle   = "Flat"
$btnLink.BackColor   = $C_DARK
$btnLink.ForeColor   = $C_WHITE
$btnLink.TextAlign   = "MiddleLeft"
$btnLink.Padding     = New-Object System.Windows.Forms.Padding(10, 0, 0, 0)
$btnLink.FlatAppearance.BorderSize = 0
$btnLink.Cursor      = [System.Windows.Forms.Cursors]::Hand
$btnLink.Visible     = $false
$btnLink.Add_Click({
    if ($script:currentStepData.LinkUrl) {
        Start-Process $script:currentStepData.LinkUrl
    }
})

# sep1, lblNote, and btnAction positions are set dynamically in Show-Step
$sep1 = New-Object System.Windows.Forms.Panel
$sep1.Size      = New-Object System.Drawing.Size(644, 1)
$sep1.BackColor = $C_BORDER

$lblNote = New-Object System.Windows.Forms.Label
$lblNote.Size      = New-Object System.Drawing.Size(644, 90)
$lblNote.Font      = $F_SM
$lblNote.ForeColor = $C_MUTED
$lblNote.AutoSize  = $false

$btnAction = New-Object System.Windows.Forms.Button
$btnAction.Size        = New-Object System.Drawing.Size(280, 38)
$btnAction.Font        = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$btnAction.FlatStyle   = "Flat"
$btnAction.BackColor   = $C_DARK2
$btnAction.ForeColor   = $C_WHITE
$btnAction.FlatAppearance.BorderSize = 0
$btnAction.Cursor      = [System.Windows.Forms.Cursors]::Hand
$btnAction.Visible     = $false

$pnlContent.Controls.AddRange(@($lblBadge, $lblStepTitle, $lblDesc, $btnLink, $sep1, $lblNote, $btnAction))

# ---- Status panel -------------------------------------------------------------

$pnlStatus = New-Object System.Windows.Forms.Panel
$pnlStatus.Location  = New-Object System.Drawing.Point(0, 448)
$pnlStatus.Size      = New-Object System.Drawing.Size(700, 80)
$pnlStatus.BackColor = $C_AMBER_B

$lblStatusIcon = New-Object System.Windows.Forms.Label
$lblStatusIcon.Location  = New-Object System.Drawing.Point(20, 14)
$lblStatusIcon.Size      = New-Object System.Drawing.Size(28, 28)
$lblStatusIcon.Font      = New-Object System.Drawing.Font("Segoe UI", 14, [System.Drawing.FontStyle]::Bold)
$lblStatusIcon.ForeColor = $C_AMBER
$lblStatusIcon.TextAlign = "MiddleCenter"
$lblStatusIcon.Text      = "?"

$lblStatusMsg = New-Object System.Windows.Forms.Label
$lblStatusMsg.Location  = New-Object System.Drawing.Point(54, 8)
$lblStatusMsg.Size      = New-Object System.Drawing.Size(630, 64)
$lblStatusMsg.Font      = $F_SM
$lblStatusMsg.ForeColor = $C_AMBER
$lblStatusMsg.AutoSize  = $false

$pnlStatus.Controls.AddRange(@($lblStatusIcon, $lblStatusMsg))

# ---- Footer -------------------------------------------------------------------

$pnlFooter = New-Object System.Windows.Forms.Panel
$pnlFooter.Location  = New-Object System.Drawing.Point(0, 548)
$pnlFooter.Size      = New-Object System.Drawing.Size(700, 56)
$pnlFooter.BackColor = $C_WHITE

$sep2 = New-Object System.Windows.Forms.Panel
$sep2.Location  = New-Object System.Drawing.Point(0, 0)
$sep2.Size      = New-Object System.Drawing.Size(700, 1)
$sep2.BackColor = $C_BORDER

$btnCheck = New-Object System.Windows.Forms.Button
$btnCheck.Location    = New-Object System.Drawing.Point(20, 10)
$btnCheck.Size        = New-Object System.Drawing.Size(140, 36)
$btnCheck.Text        = "Check Again"
$btnCheck.Font        = $F_BODY
$btnCheck.FlatStyle   = "Flat"
$btnCheck.BackColor   = $C_BG
$btnCheck.ForeColor   = $C_DARK
$btnCheck.FlatAppearance.BorderColor = $C_BORDER
$btnCheck.Cursor      = [System.Windows.Forms.Cursors]::Hand

$btnNext = New-Object System.Windows.Forms.Button
$btnNext.Location    = New-Object System.Drawing.Point(540, 10)
$btnNext.Size        = New-Object System.Drawing.Size(140, 36)
$btnNext.Text        = "Next Step  >"
$btnNext.Font        = New-Object System.Drawing.Font("Segoe UI", 9, [System.Drawing.FontStyle]::Bold)
$btnNext.FlatStyle   = "Flat"
$btnNext.BackColor   = $C_BLUE
$btnNext.ForeColor   = $C_WHITE
$btnNext.FlatAppearance.BorderSize = 0
$btnNext.Cursor      = [System.Windows.Forms.Cursors]::Hand
$btnNext.Enabled     = $false

$pnlFooter.Controls.AddRange(@($sep2, $btnCheck, $btnNext))

# ---- Assemble form ------------------------------------------------------------

$form.Controls.AddRange(@($pnlHeader, $pnlProgress, $pnlContent, $pnlStatus, $pnlFooter))

# ---- State --------------------------------------------------------------------

$script:currentStep     = 0
$script:currentStepData = $script:steps[0]

# ---- UI functions -------------------------------------------------------------

function Set-StatusPending {
    $pnlStatus.BackColor     = $C_AMBER_B
    $lblStatusIcon.ForeColor = $C_AMBER
    $lblStatusMsg.ForeColor  = $C_AMBER
    $lblStatusIcon.Text      = "?"
    $lblStatusMsg.Text       = "Click Check to verify this step, or complete any required actions above first."
    $btnNext.Enabled         = $false
    $btnNext.BackColor       = [System.Drawing.Color]::FromArgb(148, 163, 184)
}

function Set-StatusOk($msg, $version) {
    $pnlStatus.BackColor     = $C_GREEN_B
    $lblStatusIcon.ForeColor = $C_GREEN
    $lblStatusMsg.ForeColor  = $C_GREEN
    $lblStatusIcon.Text      = [char]0x2713
    $extra = if ($version) { "  ($version)" } else { "" }
    $lblStatusMsg.Text       = "$msg$extra"
    $btnNext.Enabled         = $true
    $btnNext.BackColor       = $C_BLUE
}

function Set-StatusErr($msg) {
    $pnlStatus.BackColor     = $C_RED_B
    $lblStatusIcon.ForeColor = $C_RED
    $lblStatusMsg.ForeColor  = $C_RED
    $lblStatusIcon.Text      = [char]0x2717
    $lblStatusMsg.Text       = $msg
    $btnNext.Enabled         = $false
    $btnNext.BackColor       = [System.Drawing.Color]::FromArgb(148, 163, 184)
}

function Show-Step($index) {
    $script:currentStep     = $index
    $script:currentStepData = $script:steps[$index]
    $step = $script:currentStepData

    $lblStepInfo.Text = "Step $($index + 1) of $($script:steps.Count)"

    # Progress bar fills proportionally and reaches full width on the last step.
    # Fix: original used ($index / Count) which never hit 100% on the final step.
    $filled = [int]((($index + 1) / $script:steps.Count) * 700)
    $pnlProgressFill.Width = [Math]::Max($filled, 0)

    $lblBadge.Text     = $step.Icon
    $lblStepTitle.Text = $step.Title
    $lblDesc.Text      = $step.Desc

    # Reposition separator, note, and action button based on link button visibility.
    # Fix: leaving them at hardcoded positions caused a large empty gap on steps
    # that have no link button.
    if ($step.LinkUrl) {
        $btnLink.Text       = "  $($step.LinkText)  [open in browser]"
        $btnLink.Visible    = $true
        $sep1.Location      = New-Object System.Drawing.Point(28, 198)
        $lblNote.Location   = New-Object System.Drawing.Point(28, 208)
        $btnAction.Location = New-Object System.Drawing.Point(28, 310)
    } else {
        $btnLink.Visible    = $false
        $sep1.Location      = New-Object System.Drawing.Point(28, 148)
        $lblNote.Location   = New-Object System.Drawing.Point(28, 158)
        $btnAction.Location = New-Object System.Drawing.Point(28, 262)
    }

    $lblNote.Text = $step.Note

    if ($step.ActionLabel) {
        $btnAction.Text    = "  " + $step.ActionLabel
        $btnAction.Visible = $true
    } else {
        $btnAction.Visible = $false
    }

    $btnNext.Text = if ($index -eq ($script:steps.Count - 1)) { "Finish" } else { "Next Step  >" }

    Set-StatusPending
    $form.Refresh()
}

function Run-CurrentCheck {
    $step = $script:steps[$script:currentStep]

    $pnlStatus.BackColor     = [System.Drawing.Color]::FromArgb(239, 246, 255)
    $lblStatusIcon.ForeColor = $C_BLUE
    $lblStatusMsg.ForeColor  = $C_BLUE
    $lblStatusIcon.Text      = "..."
    $lblStatusMsg.Text       = "Checking - please wait..."
    $btnCheck.Enabled        = $false
    $form.Refresh()

    $passed  = Invoke-Check $step.Check
    $version = try { & $step.Version } catch { "" }

    $btnCheck.Enabled = $true

    if ($passed) {
        Set-StatusOk $step.OkMsg $version
    } else {
        # ErrMsg may be a static string or a ScriptBlock that generates a dynamic message
        $msg = if ($step.ErrMsg -is [ScriptBlock]) { & $step.ErrMsg } else { $step.ErrMsg }
        Set-StatusErr $msg
    }
}

# ---- Button events ------------------------------------------------------------

$btnCheck.Add_Click({ Run-CurrentCheck })

$btnNext.Add_Click({
    $next = $script:currentStep + 1
    if ($next -ge $script:steps.Count) {
        Start-Process "http://localhost:5174"
        $form.Close()
    } else {
        Show-Step $next
    }
})

$btnAction.Add_Click({
    # Use $script:steps/$script:currentStep - not bare $steps/$currentStep -
    # to guarantee the correct scope from inside a WinForms event handler.
    $step = $script:steps[$script:currentStep]
    if (-not $step.Action) { return }

    $btnAction.Enabled = $false
    $btnAction.Text    = "  Running..."
    $form.Refresh()

    Run-Action $step.Action

    $btnAction.Enabled = $true
    $btnAction.Text    = "  " + $step.ActionLabel

    Run-CurrentCheck
})

# ---- Launch -------------------------------------------------------------------

try {
    Show-Step 0
    [void]$form.ShowDialog()
} catch {
    [System.Windows.Forms.MessageBox]::Show(
        "The setup wizard encountered an error:`n`n$($_.Exception.Message)`n`nTry right-clicking Install.bat and choosing 'Run as administrator'.",
        "Document Retrieval System Setup - Error",
        [System.Windows.Forms.MessageBoxButtons]::OK,
        [System.Windows.Forms.MessageBoxIcon]::Error
    )
}
