# Keeps the IBKR live Greeks tracker running (market hours only; idles otherwise).
# Started at logon by the "IBKR live Greeks" scheduled task. Output in ibkr_live.log.
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
while ($true) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- starting IBKR live tracker ---" | Out-File -FilePath ibkr_live.log -Append -Encoding utf8
    py flow_logger.py --ibkr-live 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath ibkr_live.log -Append -Encoding utf8
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- tracker stopped (exit $LASTEXITCODE); restarting in 60 s ---" | Out-File -FilePath ibkr_live.log -Append -Encoding utf8
    Start-Sleep -Seconds 60
}
