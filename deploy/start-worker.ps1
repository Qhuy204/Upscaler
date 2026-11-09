# Starts the encode worker (download -> ffmpeg/GPU encode -> upload). Restarts it if it exits.
# Webhooks and R2 cleanup run in the aux-worker container (deploy/docker-compose.yml); if no aux
# worker is alive, this worker does them itself.
# Logs: <project>\logs\worker-encode.log
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
while ($true) {
    & "$root\venv\Scripts\python.exe" -m server.run_worker --role encode --name "$env:COMPUTERNAME-encode"
    "$(Get-Date -Format s) worker exited with $LASTEXITCODE, restarting in 5s" | Add-Content "$root\logs\supervisor.log"
    Start-Sleep -Seconds 5
}
