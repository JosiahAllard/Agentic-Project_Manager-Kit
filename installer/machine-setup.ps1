# Sets this machine up for the kit, as one reviewed command (docs/machine-setup.md). A wrapper: the
# work is tools\machine_setup.py. It writes nothing into any repository: a repository works with the
# kit with nothing committed in it (installer\new-repo.ps1 is the opt-in committed setup).
#
#   powershell -NoProfile -File installer\machine-setup.ps1 [-OwnerEmail <email>] [-PmSettings <file>]
#                                [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>]
#                                [-RegisterSweepTask] [-Shortcut] [-Apply]
#   powershell -NoProfile -File installer\machine-setup.ps1 -OldKit <old kit clone> [-OwnerEmail <email>]
#                                [-ConfigDir <folder>,<folder>] [-AgentTemp <folder>]
#                                [-RegisterSweepTask] [-Apply]
#
# The default (no -OldKit; -Fresh is its old name and still accepted): the guard's entry in each
# account's settings.json, the kit plugin through the claude CLI per account, the owner's email in
# the machine config (-OwnerEmail, else the machine config's, else `git config user.email`, shown in
# the dry run to confirm), and the agents' temp folder. -PmSettings writes the PM SessionStart hook
# to that file; -Shortcut makes the machine schedule shortcut. -OldKit switches the config folders
# from an older kit clone instead: links, settings, the owner's email, the extras.
# -RegisterSweepTask, with -Apply, registers the agent-temp-sweep scheduled task (the owner's act);
# without it the plan prints the line that registers it.
#
# Dry run by default: prints every change it would make. -Apply makes them, writing a .bak beside
# each settings.json and renaming any real folder aside (never deleting one). A second run reports
# nothing to do. -ConfigDir defaults to the plugin's account folders: ~\.claude, every ~\.claude-*
# folder but the machine config's own, and each folder of the launcher's accounts and orgs (with
# -OldKit, the first two; docs/machine-setup.md, its "Config folders" item). Under -File, "a,b"
# arrives as one string, so each value is split on commas (a folder name with a comma cannot be
# passed). -HomeDir and -Desktop are for tests (a fake home folder).
#
# The tool runs under -Python <python.exe> when given (a test passes its own interpreter, so a temp
# LOCALAPPDATA never reaches the Python install manager's `python` alias), else under the `python`
# on PATH.
#
# Exit: 0 dry run printed, changes made or nothing to do; 1 refused or stopped; 2 usage (both -OldKit
# and -Fresh, -PmSettings with -OldKit, -Python not a file, no python on PATH, no
# tools\machine_setup.py).
param(
    [string]$OldKit = '',
    [switch]$Fresh,
    [string]$OwnerEmail = '',
    [string]$PmSettings = '',
    [string[]]$ConfigDir = @(),
    [string]$AgentTemp = '',
    [string]$HomeDir = '',
    [string]$Desktop = '',
    [string]$Python = '',
    [switch]$RegisterSweepTask,
    [switch]$Shortcut,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
if ($OldKit -and $Fresh) {
    Write-Host 'machine-setup: usage - give -OldKit <old kit clone> or -Fresh, not both'; exit 2
}
if ($PmSettings -and $OldKit) { Write-Host 'machine-setup: usage - -PmSettings goes with the default mode only, not -OldKit'; exit 2 }
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
if ($OldKit) { $argv += @('--old-kit', $OldKit) }
if ($OwnerEmail) { $argv += @('--owner-email', $OwnerEmail) }
if ($PmSettings) { $argv += @('--pm-settings', $PmSettings) }
foreach ($d in ($ConfigDir | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() })) {
    $argv += @('--config-dir', $d.Trim())
}
if ($AgentTemp) { $argv += @('--agent-temp', $AgentTemp) }
if ($HomeDir) { $argv += @('--home', $HomeDir) }
if ($Desktop) { $argv += @('--desktop', $Desktop) }
if ($RegisterSweepTask) { $argv += '--register-sweep-task' }
if ($Shortcut) { $argv += '--shortcut' }
if ($Apply) { $argv += '--apply' }

# Native calls run under Continue: Windows PowerShell 5.1 turns a native stderr line into an error
# record, which Stop would throw on.
$ErrorActionPreference = 'Continue'
& $exe @argv
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
