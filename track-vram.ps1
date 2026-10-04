$logPath = Join-Path $PSScriptRoot "vram_log.csv"

"timestamp,memory.used,memory.free,memory.total,gpu.util" | Out-File $logPath -Encoding ascii

while ($true) {
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss.fff"
  $row = nvidia-smi --query-gpu=memory.used,memory.free,memory.total,utilization.gpu --format=csv,noheader,nounits
  "$timestamp,$row" | Out-File $logPath -Append -Encoding ascii
  Start-Sleep -Milliseconds 500
}
