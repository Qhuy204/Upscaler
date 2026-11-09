# Registers the API, the worker and the tunnel to start at logon (run once, in an elevated PowerShell).
# Runs as the current user so the GPU encoder and the venv are available. Works from any project folder.
#   .\deploy\install-tasks.ps1                  # main machine: api + worker + tunnel
#   .\deploy\install-tasks.ps1 -Only worker     # AMD machine: API is in Docker, tunnel has its own task
param([ValidateSet("api", "worker", "tunnel")][string[]]$Only = @("api", "worker", "tunnel"))
$root = Split-Path $PSScriptRoot -Parent
$user = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
foreach ($name in $Only) {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\deploy\start-$name.ps1`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -Priority 4  # default 7 = BelowNormal: Windows parks ffmpeg on E-cores/efficiency mode, ~8x slower
    Register-ScheduledTask -TaskName "Upscale-$name" -Action $action -Trigger $trigger -Settings $settings `
        -User $user -RunLevel Limited -Force | Out-Null
    "registered Upscale-$name"
}
"Start now:  " + (($Only | ForEach-Object { "Start-ScheduledTask Upscale-$_" }) -join "; ")
