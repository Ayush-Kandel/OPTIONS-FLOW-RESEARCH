# Saves a Discord webhook URL for each bot channel (as Windows user environment variables),
# then sends a test message from every bot. Press Enter to skip a channel you haven't made yet.
$channels = [ordered]@{
    'DISCORD_WEBHOOK_SCOUT'     = '#scout-live-flow     (Scout - live capture, hourly updates)'
    'DISCORD_WEBHOOK_JUDGES'    = '#judges              (Hermes & Qwen accuracy)'
    'DISCORD_WEBHOOK_ARENA'     = '#arena-models        (nightly model contest)'
    'DISCORD_WEBHOOK_SIMULATOR' = '#simulator-backtest  (backtest results)'
    'DISCORD_WEBHOOK_ANALYST'   = '#analyst-research    (what predicts gains)'
    'DISCORD_WEBHOOK_AUDITOR'   = '#auditor-integrity   (data integrity checks)'
}
Write-Host "`nPaste each channel's webhook URL (right-click to paste), or press Enter to skip.`n"
foreach ($name in $channels.Keys) {
    Write-Host $channels[$name] -ForegroundColor Cyan
    $url = (Read-Host '  Webhook URL').Trim()
    if (-not $url) { Write-Host "  skipped`n"; continue }
    if ($url -notmatch '^https://(discord|discordapp)\.com/api/webhooks/\d+/\S+$') {
        Write-Host "  That doesn't look like a Discord webhook URL - skipped. Copy it again from Discord.`n" -ForegroundColor Yellow
        continue
    }
    [Environment]::SetEnvironmentVariable($name, $url, 'User')
    Write-Host "  saved`n" -ForegroundColor Green
}
Clear-History   # keep the URLs out of this window's command history
Write-Host "Sending a test message from every bot..."
$env:PYTHONIOENCODING = 'utf-8'
Set-Location -Path $PSScriptRoot
py flow_logger.py --test-discord
Write-Host "`nDone. Check each Discord channel for its bot's hello message."
