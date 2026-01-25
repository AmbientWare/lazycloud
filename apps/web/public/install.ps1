# LazyCloud Install Script for Windows (PowerShell)
# Usage: irm https://lazycloud.dev/install.ps1 | iex

$ErrorActionPreference = "Stop"

$LAZYCLOUD_VERSION = if ($env:LAZYCLOUD_VERSION) { $env:LAZYCLOUD_VERSION } else { "latest" }
$DEPOT_VERSION = "2.100.12"
$INSTALL_DIR = "$env:LOCALAPPDATA\Programs\lazycloud"
$VERBOSE = $env:VERBOSE -eq "true"

function Write-Info { param($Message) Write-Host $Message -ForegroundColor Cyan }
function Write-Success { param($Message) Write-Host "✓ $Message" -ForegroundColor Green }
function Write-Err { param($Message) Write-Host "✗ $Message" -ForegroundColor Red; exit 1 }
function Write-Debug { param($Message) if ($VERBOSE) { Write-Host "  $Message" } }

function Get-Architecture {
    $arch = [System.Environment]::GetEnvironmentVariable("PROCESSOR_ARCHITECTURE")
    switch ($arch) {
        "AMD64" { return "amd64" }
        "ARM64" { return "arm64" }
        default { Write-Err "Unsupported architecture: $arch" }
    }
}

function Test-Checksum {
    param($FilePath, $ExpectedHash)
    $actualHash = (Get-FileHash $FilePath -Algorithm SHA256).Hash.ToLower()
    if ($actualHash -ne $ExpectedHash.ToLower()) {
        Write-Err "Checksum verification failed for $(Split-Path $FilePath -Leaf)"
    }
}

function Install-LazyCloud {
    $arch = Get-Architecture

    if ($LAZYCLOUD_VERSION -eq "latest") {
        $baseUrl = "https://github.com/AmbientWare/lazycloud-releases/releases/latest/download"
    } else {
        $baseUrl = "https://github.com/AmbientWare/lazycloud-releases/releases/download/$LAZYCLOUD_VERSION"
    }

    $filename = "lazycloud-windows-$arch.zip"
    $url = "$baseUrl/$filename"
    $checksumUrl = "$baseUrl/$filename.sha256"

    New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

    $tempDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\lazycloud-install-cli"
    $zipPath = "$tempDir\$filename"
    $checksumPath = "$tempDir\$filename.sha256"

    Write-Debug "Downloading $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
        Invoke-WebRequest -Uri $checksumUrl -OutFile $checksumPath -UseBasicParsing
    } catch {
        Write-Err "Download failed: $_"
    }

    $expectedChecksum = (Get-Content $checksumPath -Raw).Trim().Split()[0]
    Test-Checksum -FilePath $zipPath -ExpectedHash $expectedChecksum

    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    $lazyCloudExe = Get-ChildItem -Path $tempDir -Recurse -Filter "lazycloud.exe" | Select-Object -First 1
    if (-not $lazyCloudExe) {
        Write-Err "Could not find lazycloud.exe in archive"
    }

    Copy-Item $lazyCloudExe.FullName -Destination "$INSTALL_DIR\lazycloud.exe" -Force
    Remove-Item -Recurse -Force $tempDir
}

function Install-Depot {
    $arch = Get-Architecture
    $filename = "depot_${DEPOT_VERSION}_windows_${arch}.zip"
    $url = "https://github.com/depot/cli/releases/download/v$DEPOT_VERSION/$filename"

    New-Item -ItemType Directory -Force -Path $INSTALL_DIR | Out-Null

    $tempDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\lazycloud-install-depot"
    $zipPath = "$tempDir\$filename"

    Write-Debug "Downloading $url"
    try {
        Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    } catch {
        Write-Err "Download failed: $_"
    }

    Expand-Archive -Path $zipPath -DestinationPath $tempDir -Force

    $depotExe = Get-ChildItem -Path $tempDir -Recurse -Filter "depot.exe" | Select-Object -First 1
    if (-not $depotExe) {
        Write-Err "Could not find depot.exe in archive"
    }

    Copy-Item $depotExe.FullName -Destination "$INSTALL_DIR\depot.exe" -Force
    Remove-Item -Recurse -Force $tempDir
}

function Add-ToPath {
    $currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($currentPath -like "*$INSTALL_DIR*") {
        return
    }

    $newPath = "$INSTALL_DIR;$currentPath"
    [Environment]::SetEnvironmentVariable("PATH", $newPath, "User")
    $env:PATH = "$INSTALL_DIR;$env:PATH"
    Write-Debug "Added to PATH"
}

function Main {
    Write-Host ""
    Write-Info "Installing LazyCloud..."
    Write-Host ""

    Install-LazyCloud
    Install-Depot
    Add-ToPath

    Write-Success "Installation complete!"
    Write-Host ""
    Write-Host "Restart your terminal to use lazycloud."
    Write-Host ""
}

Main
