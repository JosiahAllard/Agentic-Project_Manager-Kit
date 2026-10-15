# The committed repo setup's PowerShell front end (docs/install.md "Committed setup (--repo)"). The
# work is `tools\machine_setup.py --repo <Target>` (kitlib\machinesetup\repo.py): this script turns
# its parameters into that call and adds nothing to what it does. A repository needs none of this to
# work with the kit (plan decision 27): it is for a repository that wants its kit settings committed.
#
#   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner>
#                                -AgentEmail <agents> [-MainBranch main] [-RepoName <name>]
#                                [-Stack python] [-PmData <folder> [-PrivateRemote <url>]] [-Stubs] [-Apply]
#
# What it writes into the checkout (each listed in kitlib\machinesetup\repo.py): .kit.json, the
# .gitignore and .gitattributes lines (only the missing ones appended to an existing file), a starter
# docs\INDEX.md (only if absent; it opts the repository into the doc index check), with -Stack the
# stack's tools\gate.json, tools files and skills, with -Stubs the kit tool stubs, and the
# autoMemoryDirectory in .claude\settings.json when the file lacks it (<drive>/Agent-memory/<repo>;
# kitlib\stubs\memory.py; the owner commits it). -PmData runs
# the kit's `tools\pm_data.py init` after (the KIT's PM-data branch, docs/pm-data-branch.md). Dry run
# by default; -Apply writes. It never overwrites.
#
# This script's own part: -Target and -PmData are taken from the PowerShell location (which differs
# from the process folder once a session has used Set-Location); a trailing `\` is trimmed from them
# and from -PrivateRemote (Windows PowerShell 5.1 passes a quoted argument that ends in `\` with its
# closing quote escaped), and a double quote in any parameter is refused (5.1 does not escape one); the tool prints in the console's encoding; and with no
# python on PATH it refuses before anything runs.
#
# Exit: the tool's: 0 dry run printed or files written, 1 refused or a write failed, 2 usage; with
# -PmData and -Apply, a non-zero exit of pm_data.py init (1 refused, 2 configuration, 3 made locally
# but the push failed, 4 stopped part-way, 5 crashed, 130 Ctrl+C) after the target's files were written.
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$OwnerEmail,
    [Parameter(Mandatory = $true)][string]$AgentEmail,
    [string]$MainBranch = 'main',
    [string]$RepoName = '',
    [string]$Stack = '',
    [string]$PmData = '',
    [string]$PrivateRemote = '',
    [switch]$Stubs,
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$kit = Split-Path $PSScriptRoot -Parent
$tool = Join-Path $kit 'tools\machine_setup.py'
function Fail([string]$msg, [int]$code = 1) { Write-Host "new-repo: REFUSED - $msg"; exit $code }

if (-not (Test-Path -LiteralPath $tool)) { Fail "the kit at $kit has no tools\machine_setup.py" 2 }
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "python is not on PATH; the kit's tools need it (nothing was written)" 2 }
$PmData = $PmData.Trim(); $PrivateRemote = $PrivateRemote.Trim()
# Windows PowerShell 5.1 passes a native argument with a space inside plain double quotes without
# escaping one inside it, so a `"` in any value could split it into more arguments (an --apply the
# command line does not show, say): refused in every parameter.
foreach ($v in @($Target, $OwnerEmail, $AgentEmail, $MainBranch, $RepoName, $Stack, $PmData, $PrivateRemote)) {
    if ($v -and $v.Contains('"')) { Fail "no parameter may contain a double quote ($v)" 2 }
}
function Trim-Slash([string]$p) {
    $t = $p.TrimEnd('\')
    if ($t -match '^[A-Za-z]:$') { return $t + '\' }
    if ($t) { return $t } else { return $p }
}
function Full-Path([string]$p) {
    [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p))
}

$argv = @($tool, '--kit', $kit, '--repo', (Trim-Slash (Full-Path $Target)),
          '--owner-email', $OwnerEmail, '--agent-email', $AgentEmail)
# Windows PowerShell 5.1 drops an empty native argument: each optional one only when it has a value.
if ($MainBranch) { $argv += @('--main-branch', $MainBranch) }
if ($RepoName) { $argv += @('--repo-name', $RepoName) }
if ($Stack) { $argv += @('--stack', $Stack) }
if ($PmData) { $argv += @('--pm-data', (Trim-Slash (Full-Path $PmData))) }
if ($PrivateRemote) { $argv += @('--private-remote', (Trim-Slash $PrivateRemote)) }
if ($Stubs) { $argv += '--stubs' }
if ($Apply) { $argv += '--apply' }

# The tool prints in the console's encoding, so a name outside the code page reads back as written.
$env:PYTHONIOENCODING = "$([Console]::OutputEncoding.WebName):replace"
# Native calls run under Continue: Windows PowerShell 5.1 turns a native stderr line into an error
# record, which Stop would throw on.
$ErrorActionPreference = 'Continue'
& python @argv
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 1 }
exit $code
