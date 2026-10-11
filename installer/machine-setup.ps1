# Switches this machine's Claude config folders from an older kit clone to this kit checkout, or sets
# up a fresh machine that has none, as one reviewed command (docs/machine-setup.md). A wrapper: the
# work is tools\machine_setup.py.
#
#   powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone>
#                                [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-Apply]
#   powershell -NoProfile -File installer\machine-setup.ps1 -Fresh [-PmSettings <file>]
#                                [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-Apply]
#
# Dry run by default: prints every link, settings.json diff and extra it would change. -Apply makes
# them, writing a .bak beside each settings.json and renaming any real folder aside (never deleting
# one). A second run reports nothing to do. -Fresh (no older kit clone) only appends: guard.py's entry in each
# account's settings.json, the PM SessionStart hook in -PmSettings alone, and the kit plugin through
# the claude CLI per account. -ConfigDir defaults to ~\.claude and every ~\.claude-* folder but the
# machine config's own, and under -Fresh to the plugin's account folders: those plus each folder of
# the launcher's accounts and orgs (docs/machine-setup.md, its "Config folders" item). Under -File, "a,b" arrives as one string, so each
# value is split on commas (a folder name with a comma cannot be passed). -HomeDir and -Desktop are
# for tests (a fake home folder).
#
# The tool runs under -Python <python.exe> when given (a test passes its own interpreter, so a temp
# LOCALAPPDATA never reaches the Python install manager's `python` alias), else under the `python`
# on PATH.
#
# Exit: 0 dry run printed, changes made or nothing to do; 1 refused or stopped; 2 usage (neither or
# both of -OldKit and -Fresh, -PmSettings without -Fresh, -Python not a file, no python on PATH, no
# tools\machine_setup.py).
param(
    [string]$OldKit = '',
    [switch]$Fresh,
    [string]$PmSettings = '',
    [string[]]$ConfigDir = @(),
    [string]$AgentTemp = '',
    [string]$HomeDir = '',
    [string]$Desktop = '',
    [string]$Python = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if ([bool]$OldKit -eq [bool]$Fresh) {
    Write-Host 'machine-setup: usage - give -OldKit <old kit clone> or -Fresh (not both)'; exit 2
}
if ($PmSettings -and -not $Fresh) { Write-Host 'machine-setup: usage - -PmSettings goes with -Fresh only'; exit 2 }
$kit = Split-Path $PSScriptRoot -Parent
$tool = Join-Path $kit 'tools\machine_setup.py'
if (-not (Test-Path -LiteralPath $tool)) { Write-Host "machine-setup: REFUSED - the kit at $kit has no tools\machine_setup.py"; exit 2 }
if ($Python) {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { Write-Host "machine-setup: usage - -Python $Python is not a file"; exit 2 }
    $exe = $Python
} elseif (Get-Command python -CommandType Application -ErrorAction SilentlyContinue) {
    $exe = 'python'
} else {
    Write-Host 'machine-setup: usage - no python on PATH'; exit 2
}

$argv = @($tool, '--kit', $kit)
if ($Fresh) { $argv += '--fresh' } else { $argv += @('--old-kit', $OldKit) }
if ($PmSettings) { $argv += @('--pm-settings', $PmSettings) }
foreach ($d in ($ConfigDir | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() })) {
    $argv += @('--config-dir', $d.Trim())
}
if ($AgentTemp) { $argv += @('--agent-temp', $AgentTemp) }
if ($HomeDir) { $argv += @('--home', $HomeDir) }
if ($Desktop) { $argv += @('--desktop', $Desktop) }
if ($Apply) { $argv += '--apply' }

# Native calls run under Continue: Windows PowerShell 5.1 turns a native stderr line into an error
# record, which Stop would throw on.
$ErrorActionPreference = 'Continue'
& $exe @argv
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
