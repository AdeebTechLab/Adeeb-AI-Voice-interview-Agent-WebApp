$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Tools = Join-Path $Root "tools"
$OutFile = Join-Path $Tools "cloudflared.exe"
$Uri = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
New-Item -ItemType Directory -Force -Path $Tools | Out-Null
$ProgressPreference = "SilentlyContinue"
try {
    Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile -TimeoutSec 180
    if (-not (Test-Path $OutFile) -or (Get-Item $OutFile).Length -lt 1000000) {
        throw "Downloaded file is missing or unexpectedly small."
    }
    & $OutFile --version
    if ($LASTEXITCODE -ne 0) { throw "cloudflared.exe could not run." }
    Write-Host "Cloudflared downloaded successfully to $OutFile" -ForegroundColor Green
    exit 0
} catch {
    Remove-Item -Force -ErrorAction SilentlyContinue $OutFile
    Write-Host "Cloudflared download failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Check internet access, proxy, firewall, or antivirus settings."
    exit 1
}
