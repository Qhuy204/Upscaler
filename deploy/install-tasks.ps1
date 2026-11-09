# Registers the API and the worker to start at logon (run once, in an elevated PowerShell).
# Runs as the current user so NVENC and the venv are available.
$user = "$env:USERDOMAIN\$env:USERNAME"
New-Item -ItemType Directory -Force G:\upscale\logs | Out-Null
foreach ($name in "api", "worker", "tunnel") {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File G:\upscale\deploy\start-$name.ps1"
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -Priority 4  # default 7 = BelowNormal: Windows parks ffmpeg on E-cores/efficiency mode, ~8x slower
    Register-ScheduledTask -TaskName "Upscale-$name" -Action $action -Trigger $trigger -Settings $settings `
        -User $user -RunLevel Limited -Force | Out-Null
    "registered Upscale-$name"
}
"Start now:  Start-ScheduledTask Upscale-api; Start-ScheduledTask Upscale-worker; Start-ScheduledTask Upscale-tunnel"
