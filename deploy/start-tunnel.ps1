# Runs the Cloudflare Tunnel for upscale.qhuy204.id.vn. Restarts it if it exits.
New-Item -ItemType Directory -Force G:\upscale\logs | Out-Null
while ($true) {
    & C:\cloudflared\cloudflared.exe tunnel --no-autoupdate --loglevel info --logfile G:\upscale\logs\tunnel.log `
        --config G:\upscale\deploy\cloudflared.yml run
    "$(Get-Date -Format s) tunnel exited with $LASTEXITCODE, restarting in 5s" | Add-Content G:\upscale\logs\supervisor.log
    Start-Sleep -Seconds 5
}
