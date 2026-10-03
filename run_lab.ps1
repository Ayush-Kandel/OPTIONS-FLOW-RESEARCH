# Keeps the Strategy Lab (lab.py --daemon) running nonstop at idle priority: a full strategy search
# whenever graded data changes, luck tests in between. Started at logon by the "Strategy Lab" task.
# Output in lab.log.
Set-Location -Path $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
while ($true) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- starting the Strategy Lab ---" | Out-File -FilePath lab.log -Append -Encoding utf8
    py lab.py --daemon 2>&1 | ForEach-Object { "$_" } | Out-File -FilePath lab.log -Append -Encoding utf8
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') --- lab stopped (exit $LASTEXITCODE); restarting in 60 s ---" | Out-File -FilePath lab.log -Append -Encoding utf8
    Start-Sleep -Seconds 60
}
