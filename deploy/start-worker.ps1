# Starts the encode worker (download -> ffmpeg/NVENC -> upload). Restarts it if it exits.
# Webhooks and R2 cleanup run in the aux-worker container (deploy/docker-compose.yml); if no aux
# worker is alive, this worker does them itself.
# Logs: G:\upscale\logs\worker-encode.log
Set-Location G:\upscale
while ($true) {
    & G:\upscale\venv\Scripts\python.exe -m server.run_worker --role encode --name "$env:COMPUTERNAME-encode"
    "$(Get-Date -Format s) worker exited with $LASTEXITCODE, restarting in 5s" | Add-Content G:\upscale\logs\supervisor.log
    Start-Sleep -Seconds 5
}
