# ============================================================
#  Document Retrieval System - Updater
#  Checks GitHub Releases for a newer version, downloads and
#  installs it while preserving user data.
# ============================================================

$host.UI.RawUI.WindowTitle = "Doc Retrieval System - Updater"

# ── Configuration ─────────────────────────────────────────────
$GITHUB_OWNER = "ShreyaAbhi"
$GITHUB_REPO  = "DOC_RETRIEVAL_NATIVE"
$INSTALL_DIR  = $PSScriptRoot          # where this script lives = install root

# Optional: set env var DRS_UPDATE_TOKEN for private repos
$GH_TOKEN = $env:DRS_UPDATE_TOKEN

# Items to preserve across updates (relative to $INSTALL_DIR)
$PRESERVE_FILES = @(".env")
$PRESERVE_DIRS  = @(
    "backend\venv",
    "backend\storage",
    "backend\pod_storage",
    "backend\packing_slips",
    "backend\invoices",
    "backend\documents",
    "backend\order_import"
)
$PRESERVE_PATTERNS = @("backend\*.db", "backend\*.db-shm", "backend\*.db-wal")

# ── Helpers ───────────────────────────────────────────────────

function Write-Banner {
    param([string]$msg)
    Write-Host ""
    Write-Host "  ==========================================" -ForegroundColor Cyan
    Write-Host "   $msg" -ForegroundColor Cyan
    Write-Host "  ==========================================" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step {
    param([string]$msg, [string]$status, [string]$color = "White")
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "  [$ts] " -NoNewline -ForegroundColor DarkGray
    Write-Host ("{0,-35}" -f $msg) -NoNewline -ForegroundColor White
    Write-Host $status -ForegroundColor $color
}

function Get-LocalVersion {
    $vFile = Join-Path $INSTALL_DIR "VERSION"
    if (Test-Path $vFile) {
        return (Get-Content $vFile -Raw).Trim()
    }
    return "0.0.0"
}

function Get-GithubHeaders {
    $h = @{ "User-Agent" = "DRS-Updater" }
    if ($GH_TOKEN) { $h["Authorization"] = "token $GH_TOKEN" }
    return $h
}

# ── Main ──────────────────────────────────────────────────────

Clear-Host
Write-Banner "DOCUMENT RETRIEVAL SYSTEM - UPDATER"

$localVersion = Get-LocalVersion
Write-Host "  Current version: v$localVersion" -ForegroundColor White

# --- Check GitHub for latest release ---
Write-Host "  Checking for updates..." -ForegroundColor DarkGray
$apiUrl = "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/releases/latest"
$headers = Get-GithubHeaders

try {
    $release = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 15
} catch {
    Write-Host ""
    Write-Host "  Could not reach GitHub." -ForegroundColor Red
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Check your internet connection." -ForegroundColor Yellow
    if (-not $GH_TOKEN) {
        Write-Host "  If the repo is private, set the DRS_UPDATE_TOKEN environment variable." -ForegroundColor Yellow
    }
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

$remoteVersion = ($release.tag_name -replace '^v', '').Trim()
Write-Host "  Latest version:  v$remoteVersion" -ForegroundColor White
Write-Host ""

try {
    $isNewer = [System.Version]$remoteVersion -gt [System.Version]$localVersion
} catch {
    Write-Host "  Could not parse version numbers. Aborting." -ForegroundColor Red
    Read-Host "  Press Enter to exit"
    exit 1
}

if (-not $isNewer) {
    Write-Host "  You are already on the latest version." -ForegroundColor Green
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 0
}

Write-Host "  Update available: v$localVersion --> v$remoteVersion" -ForegroundColor Yellow
Write-Host ""

# --- Find the zip asset ---
$asset = $release.assets | Where-Object { $_.name -like "DOC_RETRIEVAL_SYSTEM_*.zip" } | Select-Object -First 1
if (-not $asset) {
    Write-Host "  No zip package found in the release assets." -ForegroundColor Red
    Write-Host "  Release may not have been packaged correctly." -ForegroundColor DarkGray
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

$assetName = $asset.name
$assetSize = [math]::Round($asset.size / 1MB, 1)
Write-Host "  Package: $assetName ($assetSize MB)" -ForegroundColor White
Write-Host ""

$confirm = Read-Host "  Install this update? (Y/N)"
if ($confirm -notmatch '^[Yy]') {
    Write-Host "  Update cancelled." -ForegroundColor Yellow
    exit 0
}

Write-Host ""

# --- Stop services ---
Write-Step "Stopping services" "..." "Yellow"

# Kill backend
$proc = netstat -ano 2>$null | findstr ":8002" | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1
if ($proc -and $proc -match '^\d+$') { Stop-Process -Id $proc -Force -ErrorAction SilentlyContinue }

# Kill frontend
$proc = netstat -ano 2>$null | findstr ":5174" | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -First 1
if ($proc -and $proc -match '^\d+$') { Stop-Process -Id $proc -Force -ErrorAction SilentlyContinue }

# Kill celery
Get-Process -Name "celery" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

Start-Sleep -Seconds 2
Write-Step "Stopping services" "DONE" "Green"

# --- Download ---
Write-Step "Downloading update" "..." "Yellow"
$tmpZip = Join-Path $env:TEMP "drs_update_$remoteVersion.zip"

try {
    $dlHeaders = Get-GithubHeaders
    if ($GH_TOKEN) {
        # Private repo: use API URL with Accept header
        $dlUrl = "https://api.github.com/repos/$GITHUB_OWNER/$GITHUB_REPO/releases/assets/$($asset.id)"
        $dlHeaders["Accept"] = "application/octet-stream"
    } else {
        $dlUrl = $asset.browser_download_url
    }
    Invoke-WebRequest -Uri $dlUrl -Headers $dlHeaders -OutFile $tmpZip -UseBasicParsing
} catch {
    Write-Step "Downloading update" "FAILED" "Red"
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor DarkGray
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Step "Downloading update" "DONE" "Green"

# --- Create backup ---
$backupDir = Join-Path (Split-Path $INSTALL_DIR -Parent) "DOC_RETRIEVAL_BACKUPS"
if (-not (Test-Path $backupDir)) { New-Item -Path $backupDir -ItemType Directory -Force | Out-Null }
$backupName = "backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
$backupPath = Join-Path $backupDir $backupName

Write-Step "Creating backup" "..." "Yellow"
try {
    Copy-Item -Path $INSTALL_DIR -Destination $backupPath -Recurse -Force -Exclude @("venv", "node_modules")
} catch {
    Write-Step "Creating backup" "FAILED" "Red"
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor DarkGray
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Step "Creating backup" "DONE ($backupName)" "Green"

# --- Stage preserved items ---
$stagingDir = Join-Path $env:TEMP "drs_preserve_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
New-Item -Path $stagingDir -ItemType Directory -Force | Out-Null

Write-Step "Preserving user data" "..." "Yellow"

foreach ($f in $PRESERVE_FILES) {
    $src = Join-Path $INSTALL_DIR $f
    if (Test-Path $src) {
        $dst = Join-Path $stagingDir $f
        $dstParent = Split-Path $dst -Parent
        if (-not (Test-Path $dstParent)) { New-Item -Path $dstParent -ItemType Directory -Force | Out-Null }
        Copy-Item -Path $src -Destination $dst -Force
    }
}

foreach ($d in $PRESERVE_DIRS) {
    $src = Join-Path $INSTALL_DIR $d
    if (Test-Path $src) {
        $dst = Join-Path $stagingDir $d
        Copy-Item -Path $src -Destination $dst -Recurse -Force
    }
}

foreach ($pat in $PRESERVE_PATTERNS) {
    $parent = Join-Path $INSTALL_DIR (Split-Path $pat -Parent)
    $leaf   = Split-Path $pat -Leaf
    if (Test-Path $parent) {
        Get-ChildItem -Path $parent -Filter $leaf -ErrorAction SilentlyContinue | ForEach-Object {
            $relPath = $_.FullName.Substring($INSTALL_DIR.Length + 1)
            $dst = Join-Path $stagingDir $relPath
            $dstParent = Split-Path $dst -Parent
            if (-not (Test-Path $dstParent)) { New-Item -Path $dstParent -ItemType Directory -Force | Out-Null }
            Copy-Item -Path $_.FullName -Destination $dst -Force
        }
    }
}
Write-Step "Preserving user data" "DONE" "Green"

# --- Extract update ---
Write-Step "Installing update" "..." "Yellow"
$tmpExtract = Join-Path $env:TEMP "drs_extract_$(Get-Date -Format 'yyyyMMdd_HHmmss')"

try {
    Expand-Archive -Path $tmpZip -DestinationPath $tmpExtract -Force

    # The zip contains a DOC_RETRIEVAL_SYSTEM/ folder -- find it
    $extracted = Get-ChildItem -Path $tmpExtract -Directory | Select-Object -First 1
    if (-not $extracted) {
        throw "Zip does not contain the expected folder structure."
    }

    # Remove old app files (but not preserved items or backups)
    $itemsToRemove = @("backend\app", "backend\docs", "backend\requirements.txt",
                        "backend\Dockerfile", "backend\scripts",
                        "frontend", "scripts",
                        "LAUNCH.ps1", "LAUNCH.bat", "update.ps1", "UPDATE.bat",
                        "installer.ps1", "Install.bat", "VERSION")
    foreach ($item in $itemsToRemove) {
        $target = Join-Path $INSTALL_DIR $item
        if (Test-Path $target) {
            Remove-Item -Path $target -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # Copy new files over
    Copy-Item -Path (Join-Path $extracted.FullName "*") -Destination $INSTALL_DIR -Recurse -Force

} catch {
    Write-Step "Installing update" "FAILED" "Red"
    Write-Host "  Error: $($_.Exception.Message)" -ForegroundColor DarkGray
    Write-Host ""
    Write-Host "  Your backup is at: $backupPath" -ForegroundColor Yellow
    Write-Host "  To restore: copy backup contents back to $INSTALL_DIR" -ForegroundColor Yellow
    Read-Host "  Press Enter to exit"
    exit 1
}
Write-Step "Installing update" "DONE" "Green"

# --- Restore preserved items ---
Write-Step "Restoring user data" "..." "Yellow"
try {
    Copy-Item -Path (Join-Path $stagingDir "*") -Destination $INSTALL_DIR -Recurse -Force
} catch {
    Write-Step "Restoring user data" "WARNING" "Yellow"
    Write-Host "  Some user data may not have been restored." -ForegroundColor Yellow
    Write-Host "  Staging dir: $stagingDir" -ForegroundColor DarkGray
}
Write-Step "Restoring user data" "DONE" "Green"

# --- Cleanup ---
Remove-Item -Path $tmpZip -Force -ErrorAction SilentlyContinue
Remove-Item -Path $tmpExtract -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path $stagingDir -Recurse -Force -ErrorAction SilentlyContinue

# --- Done ---
Write-Host ""
Write-Banner "UPDATE COMPLETE"
Write-Host "  Updated: v$localVersion --> v$remoteVersion" -ForegroundColor Green
Write-Host "  Backup:  $backupPath" -ForegroundColor DarkGray
Write-Host ""

$relaunch = Read-Host "  Launch the application now? (Y/N)"
if ($relaunch -match '^[Yy]') {
    Start-Process "cmd.exe" -ArgumentList "/c `"$INSTALL_DIR\LAUNCH.bat`""
}

exit 0
