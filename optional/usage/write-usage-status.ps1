# Writes <config dir>\usage-status.json in the status line's format from figures read with get_usage.
# Usage: write-usage-status.ps1 <fivePct> <fiveResetsAtUtc or ""> <weekPct> <weekResetsAtUtc> [-ConfigDir <dir>]
#   ("" = no five-hour window open: written as null, which coordinators read as 0%)
#   -ConfigDir: the account's Claude config folder (default: .claude in the user's profile folder,
#   from the Windows known folder, not the environment). usage_watch.py reads the same file.
param(
    [Parameter(Mandatory)][int]$FivePct,
    [string]$FiveResetsAt = "",
    [Parameter(Mandatory)][int]$WeekPct,
    [Parameter(Mandatory)][string]$WeekResetsAt,
    [string]$ConfigDir = (Join-Path ([Environment]::GetFolderPath('UserProfile')) '.claude'),
    [string]$SessionId = 'review-session-get_usage'
)
$fmt = 'yyyy-MM-ddTHH:mm:ss.fffffffzzz'
function Local([string]$utc) { ([DateTimeOffset]::Parse($utc)).ToLocalTime().ToString($fmt) }
$obj = [ordered]@{
    updated_at = [DateTimeOffset]::Now.ToString($fmt)
    session_id = $SessionId
    five_hour  = $(if ($FiveResetsAt) { [ordered]@{ used_percentage = $FivePct; resets_at = (Local $FiveResetsAt) } } else { $null })
    seven_day  = [ordered]@{ used_percentage = $WeekPct; resets_at = (Local $WeekResetsAt) }
    spend_limit = $null
}
if (-not (Test-Path -LiteralPath $ConfigDir -PathType Container)) { throw "no such config folder: $ConfigDir" }
$path = Join-Path $ConfigDir 'usage-status.json'
$tmp = "$path.tmp"
[IO.File]::WriteAllText($tmp, ($obj | ConvertTo-Json -Depth 4), (New-Object Text.UTF8Encoding($false)))
Move-Item -Force -LiteralPath $tmp -Destination $path
Get-Content -LiteralPath $path -Raw
