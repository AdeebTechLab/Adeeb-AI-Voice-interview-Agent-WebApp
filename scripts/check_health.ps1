param([int]$Seconds = 3)
$deadline = (Get-Date).AddSeconds([Math]::Max(1, $Seconds))
do {
    try {
        $r = Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8000/healthz -TimeoutSec 2
        if ($r.StatusCode -eq 200) { exit 0 }
    } catch {}
    Start-Sleep -Milliseconds 800
} while ((Get-Date) -lt $deadline)
exit 1
