# Starts the HTTP API on 127.0.0.1:8080 (cloudflared forwards upscale.qhuy204.id.vn here).
# Restarts it if it exits. Logs: <project>\logs\api.log
param([int]$Port = 8080)
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
while ($true) {
    & "$root\venv\Scripts\python.exe" -m server.run_api --host 127.0.0.1 --port $Port
    "$(Get-Date -Format s) api exited with $LASTEXITCODE, restarting in 5s" | Add-Content "$root\logs\supervisor.log"
    Start-Sleep -Seconds 5
}
