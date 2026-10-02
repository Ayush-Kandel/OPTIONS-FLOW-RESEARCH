# Keeps the Trade Echo flow logger running: starts it, and restarts it 30 s after it stops.
# Started at logon by the "Trade Echo flow logger" scheduled task. Close this window to stop it.
$host.UI.RawUI.WindowTitle = 'Trade Echo flow logger'
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
while ($true) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- starting flow logger ---" | Tee-Object -FilePath logger_console.log -Append
    py flow_logger.py 2>&1 | Tee-Object -FilePath logger_console.log -Append
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- logger stopped (exit $LASTEXITCODE); restarting in 30 s ---" | Tee-Object -FilePath logger_console.log -Append
    Start-Sleep -Seconds 30
}
