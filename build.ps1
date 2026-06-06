# Maple-FunASR-Desktop Build Script
# Run from the win/ directory

Write-Host "=== FunASR Desktop Build ===" -ForegroundColor Cyan

# Check Python
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Host "Python not found!" -ForegroundColor Red
    exit 1
}

# Install build dependencies
Write-Host "[1/3] Installing dependencies..." -ForegroundColor Yellow
pip install -r requirements.txt
pip install pyinstaller

# Download ffmpeg if not present
$ffmpegPath = Join-Path $PSScriptRoot "ui\resources\ffmpeg.exe"
if (-not (Test-Path $ffmpegPath)) {
    Write-Host "[2/3] Downloading ffmpeg..." -ForegroundColor Yellow
    $ffmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $zipPath = Join-Path $env:TEMP "ffmpeg.zip"
    Invoke-WebRequest -Uri $ffmpegUrl -OutFile $zipPath
    Expand-Archive -Path $zipPath -DestinationPath $env:TEMP\ffmpeg -Force
    Copy-Item (Get-ChildItem $env:TEMP\ffmpeg\ffmpeg-*\bin\ffmpeg.exe).FullName $ffmpegPath
    Remove-Item $zipPath -Force
    Remove-Item $env:TEMP\ffmpeg -Recurse -Force
    Write-Host "ffmpeg downloaded to $ffmpegPath" -ForegroundColor Green
}

# Create LICENSE.ffmpeg if not present
$licensePath = Join-Path $PSScriptRoot "ui\resources\LICENSE.ffmpeg"
if (-not (Test-Path $licensePath)) {
    Write-Host "[2/3] Downloading ffmpeg license..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/FFmpeg/FFmpeg/master/LICENSE.md" -OutFile $licensePath
}

# Build
Write-Host "[3/3] Building with PyInstaller..." -ForegroundColor Yellow
Set-Location $PSScriptRoot
pyinstaller build.spec

Write-Host "=== Build complete! ===" -ForegroundColor Cyan
Write-Host "Output: $PSScriptRoot\dist\FunASR-Desktop\" -ForegroundColor Green
