# Switches this machine's Claude config folders from an older kit clone to this kit checkout, as one
# reviewed command (docs/machine-setup.md). A wrapper: the work is tools\machine_setup.py.
#
#   powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone>
#                                [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>] [-Apply]
#
# Dry run by default: prints every link, settings.json diff and extra it would change. -Apply makes
# them, writing a .bak beside each settings.json and renaming any real folder aside (never deleting
# one). A second run reports nothing to do. -ConfigDir defaults to ~\.claude and every ~\.claude-*
# folder; under -File, "a,b" arrives as one string, so each value is split on commas (a folder name
# with a comma cannot be passed). -HomeDir and -Desktop are for tests (a fake home folder).
#
# Exit: 0 dry run printed, changes made or nothing to do; 1 refused or stopped; 2 usage (no -OldKit,
# no python on PATH, no tools\machine_setup.py).
param(
    [string]$OldKit = '',
    [string[]]$ConfigDir = @(),
    [string]$AgentTemp = '',
    [string]$HomeDir = '',
    [string]$Desktop = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if (-not $OldKit) { Write-Host 'machine-setup: usage - -OldKit <old kit clone> is required'; exit 2 }
$kit = Split-Path $PSScriptRoot -Parent
$tool = Join-Path $kit 'tools\machine_setup.py'
if (-not (Test-Path -LiteralPath $tool)) { Write-Host "machine-setup: REFUSED - the kit at $kit has no tools\machine_setup.py"; exit 2 }
if (-not (Get-Command python -CommandType Application -ErrorAction SilentlyContinue)) {
    Write-Host 'machine-setup: usage - no python on PATH'; exit 2
}

$argv = @($tool, '--kit', $kit, '--old-kit', $OldKit)
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
& python @argv
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
