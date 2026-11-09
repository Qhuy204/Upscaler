# Runs the Cloudflare Tunnel for upscale.qhuy204.id.vn. Restarts it if it exits.
$root = Split-Path $PSScriptRoot -Parent
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
$exe = "C:\cloudflared\cloudflared.exe"
if (-not (Test-Path $exe)) { $exe = (Get-Command cloudflared).Source }
while ($true) {
    & $exe tunnel --no-autoupdate --loglevel info --logfile "$root\logs\tunnel.log" `
        --config "$root\deploy\cloudflared.yml" run
    "$(Get-Date -Format s) tunnel exited with $LASTEXITCODE, restarting in 5s" | Add-Content "$root\logs\supervisor.log"
    Start-Sleep -Seconds 5
}
