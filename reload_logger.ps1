# Restart the flow logger so it picks up code changes. run_logger.ps1 (the restart loop run by the
# "Trade Echo flow logger" task) starts it again within 30 seconds.
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='py.exe'" |
    Where-Object { $_.CommandLine -match "flow_logger\.py\s*$" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
