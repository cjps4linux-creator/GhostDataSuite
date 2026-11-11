#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install Ghost-Data Suite as a Windows service.

.DESCRIPTION
    Registers dist\ghost-data-suite.exe as a Windows service so it starts
    automatically on boot and restarts on failure.

    Prerequisites:
    - dist\ghost-data-suite.exe must be built (pyinstaller ghost-data-suite.spec)
    - Playwright Chromium must be installed: playwright install chromium
    - The service runs under LocalSystem; adjust as needed for your environment.

.PARAMETER ServiceName
    Name of the Windows service (default: GhostDataSuite).

.PARAMETER DisplayName
    Display name shown in Services.msc (default: Ghost-Data Suite).

.PARAMETER StartMode
    Start mode: Auto, Manual, or Disabled (default: Auto).

.NOTES
    This script uses sc.exe (built into Windows). No third-party tools required.
#>
param(
    [string]$ServiceName = "GhostDataSuite",
    [string]$DisplayName = "Ghost-Data Suite",
    [string]$StartMode = "Auto"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$exePath = Join-Path $scriptDir "dist\ghost-data-suite.exe"

if (-not (Test-Path $exePath)) {
    Write-Error "Executable not found: $exePath. Build it first with: pyinstaller ghost-data-suite.spec --clean --noconfirm"
    exit 1
}

# Check if service already exists
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Warning "Service '$ServiceName' already exists. Remove it first: sc delete $ServiceName"
    exit 1
}

# Create the service
$binPath = """$exePath"""
Write-Host "Creating Windows service '$ServiceName'..."
Write-Host "  Binary: $binPath"
Write-Host "  Start mode: $StartMode"

sc.exe create $ServiceName binPath= $binPath start= $StartMode displayName= $DisplayName

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to create service '$ServiceName'. sc.exe exited with code $LASTEXITCODE"
    exit 1
}

# Set description
sc.exe description $ServiceName "Ghost-Data Suite - AI Engineering Scraping Platform"

# Set recovery options: restart on failure
sc.exe failure $ServiceName reset= 86400 actions= restart/5000/restart/5000/restart/5000

Write-Host ""
Write-Host "Service '$ServiceName' installed successfully." -ForegroundColor Green
Write-Host ""
Write-Host "To start:   net start $ServiceName"
Write-Host "To stop:    net stop $ServiceName"
Write-Host "To remove:  sc delete $ServiceName"
Write-Host ""
Write-Host "Note: The .exe auto-generates GHOST_AUTH_TOKEN and SECRET_KEY at startup."
Write-Host "      For production, set them as environment variables before starting the service."
