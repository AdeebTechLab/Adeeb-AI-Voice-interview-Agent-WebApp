param([int]$Seconds = 60)
$Root = Split-Path -Parent $PSScriptRoot
$LinkFile = Join-Path $Root "CURRENT_PUBLIC_LINK.txt"
for ($i = 0; $i -lt $Seconds; $i++) {
    if (Test-Path $LinkFile) { exit 0 }
    Start-Sleep -Seconds 1
}
exit 1
