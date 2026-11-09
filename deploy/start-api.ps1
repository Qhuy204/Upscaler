# Starts the HTTP API on 127.0.0.1:8080 (cloudflared forwards upscale.qhuy204.id.vn here).
# Restarts it if it exits. Logs: G:\upscale\logs\api.log
Set-Location G:\upscale
while ($true) {
    & G:\upscale\venv\Scripts\python.exe -m server.run_api --host 127.0.0.1 --port 8080
    "$(Get-Date -Format s) api exited with $LASTEXITCODE, restarting in 5s" | Add-Content G:\upscale\logs\supervisor.log
    Start-Sleep -Seconds 5
}
