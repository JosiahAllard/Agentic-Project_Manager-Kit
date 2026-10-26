# Claude Code status line: shows plan usage, and records it where an agent can
# read it. Claude Code pipes session JSON to stdin; `rate_limits` is present only
# for claude.ai subscribers, and only after the session's first API response.
# Output file: usage-status.json in this account's config folder ($CLAUDE_CONFIG_DIR,
# default ~/.claude), read by the `coordinator` agent.
$ErrorActionPreference = 'SilentlyContinue'
$raw = [Console]::In.ReadToEnd()
try { $j = $raw | ConvertFrom-Json } catch { $j = $null }

function Window($w) {
    if ($null -eq $w) { return $null }
    $resets = $null
    if ($w.resets_at) { $resets = ([DateTimeOffset]::FromUnixTimeSeconds([long]$w.resets_at)).LocalDateTime.ToString('o') }
    [ordered]@{ used_percentage = $w.used_percentage; resets_at = $resets }
}

$limits = $null
if ($j) { $limits = $j.rate_limits }
$parts = @()
if ($j -and $j.model.display_name) { $parts += "[$($j.model.display_name)]" }

if ($limits) {
    $out = [ordered]@{
        updated_at  = (Get-Date).ToString('o')
        session_id  = $j.session_id
        five_hour   = Window $limits.five_hour
        seven_day   = Window $limits.seven_day
        spend_limit = Window $limits.spend_limit
    }
    # Each account's config folder gets its own file (CLAUDE_CONFIG_DIR, default ~/.claude).
    $cfg = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $env:USERPROFILE '.claude' }
    $path = Join-Path $cfg 'usage-status.json'
    $tmp = "$path.$PID.tmp"
    ($out | ConvertTo-Json -Depth 4) | Out-File -FilePath $tmp -Encoding utf8
    # A locked target or a race with another session fails the move silently: retry once, and
    # never leave the temp file behind.
    Move-Item -Force $tmp $path
    if (Test-Path -LiteralPath $tmp) {
        Start-Sleep -Milliseconds 100
        Move-Item -Force $tmp $path
    }
    if (Test-Path -LiteralPath $tmp) { Remove-Item -Force -LiteralPath $tmp }
    # Sweep this script's own leftovers (older runs): one filter over this folder, no recursion.
    $stale = (Get-Date).AddMinutes(-5)
    Get-ChildItem -LiteralPath $cfg -Filter 'usage-status.json.*.tmp' -File |
        Where-Object { $_.Name -match '^usage-status\.json\.\d+\.tmp$' -and $_.LastWriteTime -lt $stale } |
        Remove-Item -Force
    if ($limits.five_hour) { $parts += ('5h {0:N0}%' -f $limits.five_hour.used_percentage) }
    if ($limits.seven_day) { $parts += ('7d {0:N0}%' -f $limits.seven_day.used_percentage) }
} else {
    $parts += 'usage n/a'
}
if ($j -and $null -ne $j.context_window.used_percentage) { $parts += ('ctx {0:N0}%' -f $j.context_window.used_percentage) }
Write-Output ($parts -join '  ')
