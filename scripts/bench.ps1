param($Name, $InputFile, $Extra = "")
# Runs upscale.py while logging GPU (nvidia-smi) and total CPU % once per second.
$out = 'G:\upscale\output'; New-Item -ItemType Directory -Force $out | Out-Null
$gpuLog = "$out\$Name-gpu.csv"; $cpuLog = "$out\$Name-cpu.csv"
$smi = Start-Process nvidia-smi -ArgumentList '--query-gpu=timestamp,utilization.gpu,memory.used,temperature.gpu,power.draw','--format=csv','--loop=1' -RedirectStandardOutput $gpuLog -NoNewWindow -PassThru
$cpu = Start-Process typeperf -ArgumentList '"\Processor(_Total)\% Processor Time"','-si','1' -RedirectStandardOutput $cpuLog -NoNewWindow -PassThru
$extraArgs = @($Extra -split ' ' | Where-Object { $_ })
$sw = [Diagnostics.Stopwatch]::StartNew()
& G:\upscale\venv\Scripts\python.exe G:\upscale\scripts\upscale.py $InputFile @extraArgs -o "$out\$Name.mp4" *> "$out\$Name-run.log"
$code = $LASTEXITCODE; $sw.Stop()
Stop-Process $smi.Id, $cpu.Id -Force
"exit=$code seconds=$($sw.Elapsed.TotalSeconds)" | Out-File "$out\$Name-done.txt"
