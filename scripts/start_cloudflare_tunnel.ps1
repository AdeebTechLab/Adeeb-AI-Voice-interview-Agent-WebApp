param(
    [Parameter(Mandatory = $true)]
    [string]$CloudflaredPath
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "cloudflared.log"
$LinkFile = Join-Path $Root "CURRENT_PUBLIC_LINK.txt"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue $LinkFile
"Cloudflare started: $(Get-Date -Format o)" | Set-Content -Encoding UTF8 $LogFile

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/healthz -TimeoutSec 2
        if ($r.StatusCode -eq 200) {
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}
if (-not $ready) {
    Write-Host "Local Adeeb server is not reachable at http://127.0.0.1:8000/healthz" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}

$found = $false
try {
    & $CloudflaredPath tunnel --url http://127.0.0.1:8000 --no-autoupdate 2>&1 | ForEach-Object {
        $line = [string]$_
        Write-Host $line
        Add-Content -Encoding UTF8 -Path $LogFile -Value $line
        if (-not $found -and $line -match 'https://[a-z0-9-]+\.trycloudflare\.com') {
            $public = $Matches[0]
            $join = "$public/join"
            @(
                "CURRENT ADEEB PUBLIC LINKS",
                "Generated: $(Get-Date -Format o)",
                "",
                "HR dashboard: $public",
                "Candidate join: $join",
                "",
                "Keep the local server and Cloudflare windows open.",
                "This temporary URL stops working when the tunnel closes."
            ) | Set-Content -Encoding UTF8 $LinkFile
            try { Set-Clipboard -Value $join } catch {}
            try { Start-Process $join } catch {}
            Write-Host ""
            Write-Host "Candidate link saved and copied:" -ForegroundColor Green
            Write-Host $join -ForegroundColor Cyan
            Write-Host ""
            $found = $true
        }
    }
    $code = $LASTEXITCODE
} catch {
    $_ | Out-String | Add-Content -Encoding UTF8 $LogFile
    Write-Host $_ -ForegroundColor Red
    $code = 1
}
Write-Host "Cloudflare tunnel stopped with exit code $code." -ForegroundColor Yellow
Read-Host "Press Enter to close this window"
exit $code
