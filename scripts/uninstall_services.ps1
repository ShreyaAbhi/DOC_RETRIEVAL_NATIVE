# ============================================================
#  Document Retrieval System — Remove Windows Services
#  Run as Administrator
# ============================================================
#Requires -RunAsAdministrator

$root = (Resolve-Path "$PSScriptRoot\..").Path
$nssm = "$root\tools\nssm\nssm.exe"
$serviceName = "DRS"

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "   DOCUMENT RETRIEVAL SYSTEM               " -ForegroundColor Cyan
Write-Host "   Service Uninstaller                     " -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $nssm)) {
    Write-Host "  ERROR: NSSM not found at $nssm" -ForegroundColor Red
    Write-Host "  Services may need to be removed manually via sc.exe" -ForegroundColor Yellow
    exit 1
}

$services = @("$serviceName-Ollama", "$serviceName-Backend", "$serviceName-Worker", "$serviceName-Beat")

foreach ($svc in $services) {
    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue
    if ($s) {
        Write-Host "  Stopping $svc..." -ForegroundColor Yellow -NoNewline
        & $nssm stop $svc 2>&1 | Out-Null
        Start-Sleep -Seconds 2
        Write-Host " stopped." -ForegroundColor Green

        Write-Host "  Removing $svc..." -ForegroundColor Yellow -NoNewline
        & $nssm remove $svc confirm 2>&1 | Out-Null
        Write-Host " removed." -ForegroundColor Green
    } else {
        Write-Host "  $svc — not installed, skipping." -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Host "  All DRS services have been removed." -ForegroundColor Green
Write-Host "  The application files are still in: $root" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Press any key to close." -ForegroundColor DarkGray
$host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown") | Out-Null
