# LazyCloud Install Script for Windows (PowerShell)
# Usage: irm https://lazycloud.dev/install.ps1 | iex

$ErrorActionPreference = "Stop"

$LAZYCLOUD_VERSION = if ($env:LAZYCLOUD_VERSION) { $env:LAZYCLOUD_VERSION } else { "latest" }
$DEPOT_VERSION = "2.100.12"
$INSTALL_DIR = "$env:LOCALAPPDATA\Programs\lazycloud"

function Write-Info { param($Message) Write-Host $Message -ForegroundColor Cyan }
function Write-Success { param($Message) Write-Host "✓ $Message" -ForegroundColor Green }
function Write-Warn { param($Message) Write-Host "⚠ $Message" -ForegroundColor Yellow }
function Write-Err { param($Message) Write-Host "✗ $Message" -ForegroundColor Red; exit 1 }

function Get-Architecture {
    $arch = [System.Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITECTURE")
    switch ($arch) {
        "AMD64" { return "amd64" }
        "ARM64" { return "arm64" }
        default { Write-Err "Unsupported architecture: $arch" }
    }
}

function Test-Command {
    param($Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

function Install-LazyCloud {
    Write-Info "Installing LazyCloud CLI..."

    $arch = Get-Architecture

    # Determine download URL
    if ($LAZYCLOUD_VERSION -eq "latest") {
        $url = "https://github.com/AmbientWare/lazycloud-releases/releases/latest/download/lazycloud-windows-$arch.zip"
    } else {
        $url = "https://github.com/AmbientWare/lazycloud-releases/releases/download/$LAZYCLOUD_VERSION/lazycloud-windows-$arch.zip"
    }

    # Create install directory
    New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

    # Download
    $tempDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\lazycloud-install-cli"
    $zipPath = "$tempDir\lazycloud.zip"

    Write-Info "Downloading from $url..."
    try {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Err "Failed to download LazyCloud CLI: $_"
    }

    # Extract
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    # Move lazycloud.exe
    $lazyCloudExe = Get-ChildItem -Path $tempDir -Recurse -Filter "lazycloud.exe" | Select-Object -First 1
    if (-not $lazyCloudExe) {
        Write-Err "Could not find lazycloud.exe in archive"
    }

    Copy-Item $lazyCloudExe.FullName -Destination "$INSTALL_DIR\lazycloud.exe" -Force

    # Cleanup
    Remove-Item -Recurse -Force $tempDir

    Write-Success "LazyCloud CLI installed to $INSTALL_DIR\lazycloud.exe"
}

function Install-Depot {
    Write-Info "Installing Depot CLI v$DEPOT_VERSION..."

    $arch = Get-Architecture
    $filename = "depot_${DEPOT_VERSION}_windows_${arch}.zip"
    $url = "https://github.com/depot/cli/releases/download/v$DEPOT_VERSION/$filename"

    # Create install directory
    New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

    # Download
    $tempDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\lazycloud-install"
    $zipPath = "$tempDir\$filename"

    Write-Info "Downloading from $url..."
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing

    # Extract
    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    # Find and move depot.exe
    $depotExe = Get-ChildItem -Path $tempDir -Recurse -Filter "depot.exe" | Select-Object -First 1
    if (-not $depotExe) {
        Write-Err "Could not find depot.exe in archive"
    }

    Copy-Item $depotExe.FullName -Destination "$INSTALL_DIR\depot.exe" -Force

    # Cleanup
    Remove-Item -Recurse -Force $tempDir

    Write-Success "Depot CLI installed to $INSTALL_DIR\depot.exe"
}

function Add-ToPath {
    # Check if already in PATH
    $currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($currentPath -like "*$INSTALL_DIR*") {
        return
    }

    Write-Info "Adding $INSTALL_DIR to PATH..."

    # Add to user PATH
    $newPath = "$INSTALL_DIR;$currentPath"
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")

    # Update current session
    $env:PATH = "$INSTALL_DIR;$env:PATH"

    Write-Success "Added to PATH"
}

function Test-Installation {
    Write-Info "Verifying installation..."

    if (Test-Command "lazycloud") {
        Write-Success "lazycloud is available"
    }
    else {
        Write-Warn "lazycloud not found in PATH. You may need to restart your terminal."
    }

    if (Test-Path "$INSTALL_DIR\depot.exe") {
        Write-Success "depot is available at $INSTALL_DIR\depot.exe"
    }
    else {
        Write-Warn "depot not found"
    }
}

function Main {
    Write-Host ""
    Write-Info "╔═══════════════════════════════════════╗"
    Write-Info "║       LazyCloud Installer             ║"
    Write-Info "╚═══════════════════════════════════════╝"
    Write-Host ""

    Install-LazyCloud
    Install-Depot
    Add-ToPath
    Test-Installation

    Write-Host ""
    Write-Success "Installation complete!"
    Write-Host ""
    Write-Info "To get started:"
    Write-Host "  1. Restart your terminal"
    Write-Host "  2. Run: lazycloud login"
    Write-Host "  3. Run: lazycloud init"
    Write-Host ""
}

Main

