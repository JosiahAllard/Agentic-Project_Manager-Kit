# Bootstraps a repository onto the kit (docs/install.md; ported from the PM kit's new-repo.ps1).
#
#   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner>
#                                -AgentEmail <agents> [-MainBranch main] [-Stack python]
#                                [-PmData <folder> [-PrivateRemote <url>]] [-Apply]
#
# The target is an existing git checkout (`git init -b main` first for a new one); the folder -Target
# names must be its top level. It writes these and nothing else (a repository holds stubs only,
# never a copy of the kit):
#   <Target>\.kit.json            "repo".main_branch, "owner".email, "owner".agent_emails, and with
#                                -Stack "gate": "tools/gate.json"
#   <Target>\<tools_dir>\<n>.py   the kit tool stubs, by `tools\kit_stubs.py write -C <Target>`
#   <Target>\.gitignore, .gitattributes   templates\common's lines (.claude/worktrees/ keeps the
#                                agents' worktrees out of the main checkout's status) plus the
#                                stack's gitignore/gitattributes lists. An existing file is never
#                                overwritten: only the lines it lacks are appended.
#   <Target>\docs\INDEX.md        a starter doc index (templates\common\INDEX.md), only if absent
#   <Target>\tools\gate.json      with -Stack: templates\stacks\<Stack>\tools\gate.json, only if absent
# It does not install the stack's CI workflows or other stack files, templates\common\CLAUDE.md,
# the agent profiles, or .claude\settings.json (docs/install.md "What the script does not install").
# -OwnerEmail and -AgentEmail are required and must differ: an owner commit (Owner-Approved:) counts
# only when its author email is "owner".email, so an empty or shared identity would let an agent's
# commit pass as the owner's.
#
# Dry run by default: prints what it would write. -Apply writes. It never overwrites: a .kit.json or
# tools\gate.json already there, or a file where a stub goes that is not a stub, refuses before
# anything is written. A missing `origin` remote (new_worktree and preflight need it) is a warning
# that says what to run.
#
# -PmData <folder> (docs/pm-data-branch.md "Other users"): after the setup above, run the kit's
# `tools\pm_data.py -C <kit> init --path <folder> [--url <PrivateRemote>]`, which makes the PM-data
# branch of the KIT's repository (not the target's), its locked worktree at <folder>, and the
# private remote when -PrivateRemote is given. The dry run prints that command and runs nothing.
# Its result is reported as pm_data.py gives it: its lines, then its exit code as this script's.
#
# Exit: 0 dry run printed or files written, 1 refused, 2 usage; with -PmData and -Apply, a non-zero
# exit of pm_data.py init (1 refused, 2 configuration, 3 made locally but the push failed, 4 stopped
# part-way, 130 Ctrl+C) after the target's files were written.
param(
    [Parameter(Mandatory = $true)][string]$Target,
    [Parameter(Mandatory = $true)][string]$OwnerEmail,
    [Parameter(Mandatory = $true)][string]$AgentEmail,
    [string]$MainBranch = 'main',
    [string]$Stack = '',
    [string]$PmData = '',
    [string]$PrivateRemote = '',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
$kit = Split-Path $PSScriptRoot -Parent
$stubTool = Join-Path $kit 'tools\kit_stubs.py'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Fail([string]$msg, [int]$code = 1) { Write-Host "new-repo: REFUSED - $msg"; exit $code }

$emailRx = '^[^@\s"\\<>$]+@[^@\s"\\<>$]+\.[^@\s"\\<>$]+$'
$OwnerEmail = $OwnerEmail.Trim(); $AgentEmail = $AgentEmail.Trim()
if ($OwnerEmail -notmatch $emailRx) { Fail "-OwnerEmail '$OwnerEmail' is not an email address" 2 }
if ($AgentEmail -notmatch $emailRx) { Fail "-AgentEmail '$AgentEmail' is not an email address" 2 }
if ($OwnerEmail -ieq $AgentEmail) {
    Fail "-OwnerEmail and -AgentEmail are the same; agents need their own identity or their commits pass as the owner's" 2
}
if ($MainBranch -notmatch '^[A-Za-z0-9][A-Za-z0-9._/-]*$') { Fail "-MainBranch '$MainBranch' is not a branch name" 2 }
$PmData = $PmData.Trim(); $PrivateRemote = $PrivateRemote.Trim()
if ($PrivateRemote -and -not $PmData) { Fail "-PrivateRemote needs -PmData <folder>" 2 }
if ($PrivateRemote.StartsWith('-')) { Fail "-PrivateRemote '$PrivateRemote' is not a url" 2 }
$pmTool = Join-Path $kit 'tools\pm_data.py'

$targetFull = [IO.Path]::GetFullPath($Target).TrimEnd('\')
$kitFull = [IO.Path]::GetFullPath($kit).TrimEnd('\') + '\'
if (($targetFull + '\').StartsWith($kitFull, [StringComparison]::OrdinalIgnoreCase)) {
    Fail "the target $targetFull is inside the kit itself"
}
if (-not (Test-Path -LiteralPath $targetFull -PathType Container)) { Fail "$targetFull is not a folder" }
# Native calls run under Continue: Windows PowerShell 5.1 turns a native stderr line into an error
# record, which Stop would throw on.
$ErrorActionPreference = 'Continue'
$top = & git -C $targetFull rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or -not $top) { Fail "$targetFull is not a git checkout (git init -b $MainBranch first)" }
$topFull = [IO.Path]::GetFullPath(($top | Select-Object -First 1).Trim()).TrimEnd('\')
if ($topFull -ine $targetFull) { Fail "$targetFull is inside the checkout $topFull; name its top level" }
if (-not (Test-Path -LiteralPath $stubTool)) { Fail "the kit at $kit has no tools\kit_stubs.py" 2 }
# The PM-data command: argv for the call, and the same quoted for the printed line.
$pmArgs = @()
$pmLine = ''
if ($PmData) {
    if (-not (Test-Path -LiteralPath $pmTool)) { Fail "the kit at $kit has no tools\pm_data.py" 2 }
    $pmPath = [IO.Path]::GetFullPath($PmData).TrimEnd('\')
    $pmArgs = @($pmTool, '-C', $kitFull.TrimEnd('\'), 'init', '--path', $pmPath)
    if ($PrivateRemote) { $pmArgs += @('--url', $PrivateRemote) }
    $pmLine = 'python ' + (($pmArgs | ForEach-Object { if ($_ -match '[\s"]') { '"' + $_ + '"' } else { $_ } }) -join ' ')
}

$kitJson = Join-Path $targetFull '.kit.json'
if (Test-Path -LiteralPath $kitJson) { Fail "$kitJson exists already; nothing was written" }

# The stack, if one was named: its templates\stacks\<Stack> folder holds the gate section and the
# gitignore/gitattributes lists.
$stackLines = @{ gitignore = @(); gitattributes = @() }
$gateSrc = $null
$gateDst = Join-Path $targetFull 'tools\gate.json'
if ($Stack) {
    if ($Stack -notmatch '^[a-z0-9]+$') { Fail "-Stack '$Stack' is not a stack name" 2 }
    $stackDir = Join-Path $kit "templates\stacks\$Stack"
    if (-not (Test-Path -LiteralPath $stackDir -PathType Container)) {
        $known = (Get-ChildItem -LiteralPath (Join-Path $kit 'templates\stacks') -Directory | ForEach-Object { $_.Name }) -join ', '
        Fail "-Stack '$Stack' is not a stack the kit has ($known)" 2
    }
    $meta = Get-Content -LiteralPath (Join-Path $stackDir 'stack.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($k in 'gitignore', 'gitattributes') { if ($meta.$k) { $stackLines[$k] = @($meta.$k) } }
    $gateSrc = Join-Path $stackDir 'tools\gate.json'
    if (-not (Test-Path -LiteralPath $gateSrc)) { Fail "the kit has no $gateSrc" 2 }
    if (Test-Path -LiteralPath $gateDst) { Fail "$gateDst exists already; nothing was written" }
}
$gateLine = if ($Stack) { "  `"gate`": `"tools/gate.json`",`n" } else { '' }
$text = "{`n" +
        "  `"repo`": {`"main_branch`": `"$MainBranch`"},`n" +
        $gateLine +
        "  `"owner`": {`"email`": `"$OwnerEmail`", `"agent_emails`": [`"$AgentEmail`"]}`n" +
        "}`n"

# Line files: the common template's lines, then the stack's. Compared with runs of spaces folded.
function Norm([string]$l) { ($l.Trim() -replace '\s+', ' ') }
function Plan-LineFile([string]$name, [string[]]$stackWant) {
    $path = Join-Path $targetFull $name
    # The kit ships the attributes template as "gitattributes": a tracked .gitattributes would apply to
    # the kit's own checkout (and the fake git of its tests refuses one).
    $tplName = if ($name -eq '.gitattributes') { 'gitattributes' } else { $name }
    $tpl = @(Get-Content -LiteralPath (Join-Path $kit "templates\common\$tplName") -Encoding UTF8)
    $stackWant = @($stackWant | Where-Object { $_ -and $_.Trim() -and -not $_.TrimStart().StartsWith('#') })
    $want = @($tpl | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') }) + @($stackWant)
    $new = -not (Test-Path -LiteralPath $path)
    $have = @()
    if (-not $new) { $have = @(Get-Content -LiteralPath $path -Encoding UTF8 | ForEach-Object { Norm $_ }) }
    $missing = @($want | Where-Object { $have -notcontains (Norm $_) })
    $body = @()
    if ($new) {
        $body += $tpl
        $extra = @($stackWant | Where-Object { $tpl -notcontains $_ })
        if ($extra.Count) { $body += '# From the stack template'; $body += $extra }
    } elseif ($missing.Count) {
        $body += '# Added by the kit installer'; $body += $missing
    }
    [pscustomobject]@{ Name = $name; Path = $path; New = $new; Missing = $missing; Body = $body }
}
$lineFiles = @((Plan-LineFile '.gitignore' $stackLines.gitignore), (Plan-LineFile '.gitattributes' $stackLines.gitattributes))
$indexDst = Join-Path $targetFull 'docs\INDEX.md'
$indexSrc = Join-Path $kit 'templates\common\INDEX.md'
$indexNew = -not (Test-Path -LiteralPath $indexDst)

# The stubs the kit would write, from kit_stubs itself (it reads the target's .kit.json, which is
# not there yet: the default tools_dir and tool list apply, as they will after -Apply).
$plan = & python $stubTool write -C $targetFull --dry-run 2>&1
$planCode = $LASTEXITCODE

# The default remote name: .kit.json "repo".remote is not set by this script.
$remoteUrl = & git -C $targetFull remote get-url origin 2>$null
$hasOrigin = ($LASTEXITCODE -eq 0 -and $remoteUrl)

$mode = if ($Apply) { 'APPLY' } else { 'DRY RUN (nothing written; add -Apply to write)' }
Write-Host "new-repo: $mode"
Write-Host "  target:  $targetFull"
Write-Host "  kit:     $($kitFull.TrimEnd('\'))"
if ($Stack) { Write-Host "  stack:   $Stack" }
Write-Host ""
Write-Host ".kit.json:"
foreach ($line in ($text.TrimEnd("`n") -split "`n")) { Write-Host "  $line" }
Write-Host ""
Write-Host "stubs:"
foreach ($line in @($plan)) { Write-Host "  $line" }
Write-Host ""
Write-Host "other files:"
foreach ($f in $lineFiles) {
    if ($f.New) { Write-Host "  would create  $($f.Path)" }
    elseif ($f.Missing.Count) { Write-Host "  would append  $($f.Path)   ($($f.Missing.Count) missing: $($f.Missing -join ' , ')); the lines it has are kept" }
    else { Write-Host "  unchanged     $($f.Path)   (has every line already)" }
}
if ($indexNew) { Write-Host "  would create  $indexDst   (starter doc index)" } else { Write-Host "  kept          $indexDst   (exists)" }
if ($Stack) { Write-Host "  would create  $gateDst   (from templates\stacks\$Stack\tools\gate.json)" }
else { Write-Host "  no gate section (no -Stack): see Next, step 3" }
Write-Host ""
if ($hasOrigin) { Write-Host "remote:   origin -> $(@($remoteUrl)[0])" }
else {
    Write-Host "WARNING:  no remote named origin. new_worktree.py and preflight.py fetch it and refuse without it."
    Write-Host "          git -C `"$targetFull`" remote add origin <url>   then   git -C `"$targetFull`" push -u origin $MainBranch"
}
if ($PmData) {
    Write-Host ""
    Write-Host "PM data (the kit's own repository, docs/pm-data-branch.md):"
    $when = if ($Apply) { 'runs' } else { 'would run' }
    Write-Host "  $when after the files above:  $pmLine"
}
if ($planCode -ne 0) { Fail "kit_stubs.py refused the stubs above (exit $planCode); nothing was written" }
if (-not $Apply) { exit 0 }

# The gate file goes first: kit_stubs reads .kit.json and refuses a "gate" path that is not there.
if ($Stack) {
    New-Item -ItemType Directory -Force -Path (Split-Path $gateDst) | Out-Null
    [IO.File]::WriteAllText($gateDst, [IO.File]::ReadAllText($gateSrc, $utf8), $utf8)
    Write-Host "  wrote    $gateDst"
}
try {
    $fs = New-Object IO.FileStream($kitJson, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try { $bytes = $utf8.GetBytes($text); $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
} catch {
    $did = if ($Stack) { "only $gateDst was written (delete it to undo)" } else { "nothing was written" }
    Fail "cannot create $kitJson - $($_.Exception.Message); $did"
}
$out = & python $stubTool write -C $targetFull 2>&1
$code = $LASTEXITCODE
foreach ($line in @($out)) { Write-Host "  $line" }
if ($code -ne 0) {
    $wrote = if ($Stack) { ".kit.json and $gateDst" } else { ".kit.json" }
    Write-Host "new-repo: STOPPED - kit_stubs.py write exited $code after $wrote were written, and any stubs listed above."
    Write-Host "  The .gitignore, .gitattributes and docs/INDEX.md were not touched. To undo: delete $wrote and the stubs listed, then run again."
    exit 1
}
foreach ($f in $lineFiles) {
    if (-not $f.Body.Count) { continue }
    $chunk = ($f.Body -join "`n") + "`n"
    if ($f.New) { [IO.File]::WriteAllText($f.Path, $chunk, $utf8); Write-Host "  wrote    $($f.Path)" }
    else {
        $old = [IO.File]::ReadAllText($f.Path)
        # Append in the file's own line ending (CRLF when it has any).
        $nl = if ($old.Contains("`r`n")) { "`r`n" } else { "`n" }
        $chunk = ($f.Body -join $nl) + $nl
        $sep = if ($old.Length -and -not $old.EndsWith("`n")) { $nl } else { '' }
        [IO.File]::AppendAllText($f.Path, $sep + $chunk, $utf8)
        Write-Host "  appended $($f.Path)   ($($f.Missing.Count) line(s))"
    }
}
if ($indexNew) {
    New-Item -ItemType Directory -Force -Path (Split-Path $indexDst) | Out-Null
    [IO.File]::WriteAllText($indexDst, [IO.File]::ReadAllText($indexSrc, $utf8), $utf8)
    Write-Host "  wrote    $indexDst"
}
Write-Host ""
Write-Host "new-repo: wrote .kit.json, the stubs and the starter files in $targetFull"
$pmCode = 0
if ($PmData) {
    Write-Host ""
    Write-Host "PM data: $pmLine"
    $pmOut = & python @pmArgs 2>&1
    $pmCode = $LASTEXITCODE
    foreach ($line in @($pmOut)) { Write-Host "  $line" }
    if ($pmCode -eq 0) { Write-Host "PM data: pm_data.py init exited 0 (done)" }
    else {
        $why = @{ 1 = 'refused, it changed nothing'; 2 = 'usage or a configuration it cannot read';
                  3 = 'made locally, the push to the private remote failed; its lines above say how to recover';
                  4 = 'stopped part-way; run it again with --adopt to finish';
                  130 = 'interrupted (Ctrl+C); the lines above are what it made' }[$pmCode]
        if (-not $why) { $why = 'an exit code pm_data.py does not document' }
        # An uncaught Python error also exits 1: never call that a refusal that changed nothing.
        if (@($pmOut | Where-Object { "$_" -match '^Traceback \(most recent call last\):' }).Count) {
            $why = 'it crashed; what it made before the error is not known, check the kit repository'
        }
        Write-Host "new-repo: STOPPED - the files above were written in $targetFull; pm_data.py init exited $pmCode ($why)."
        Write-Host "  Fix what its lines name, then run the PM-data step alone:  $pmLine"
    }
}
Write-Host "Next:"
Write-Host "  1. review .kit.json (docs/kit-json.md names every key). Commit .kit.json and tools/gate.json under the"
Write-Host "     owner's identity (owner-only paths: tools/commit.py refuses an agent's); commit the stubs, .gitignore,"
Write-Host "     .gitattributes and docs/INDEX.md as usual"
Write-Host "  2. python `"$stubTool`" check -C `"$targetFull`"   (exit 0: every stub current)"
if ($Stack) {
    Write-Host "  3. tools/gate.json is the $Stack gate: edit its globs and test command to the project; its pass check"
    Write-Host "     needs a line 'Ran N tests' with N of 1 or more, so keep at least one test or the step fails"
} else {
    Write-Host "  3. the gate: with no gate section gate.py refuses (no gate step selected). Copy a stack's"
    Write-Host "     $kit\templates\stacks\<stack>\tools\gate.json to tools\gate.json and add  `"gate`": `"tools/gate.json`"  to .kit.json,"
    Write-Host "     or write gate.steps in .kit.json (docs/kit-json.md)"
}
Write-Host "  4. git -C `"$targetFull`" config core.hooksPath `"$kit\githooks`"   (the kit's pre-push hook)"
if (-not $hasOrigin) {
    Write-Host "  5. the remote: git -C `"$targetFull`" remote add origin <url>, then push $MainBranch (no origin yet)"
}
exit $pmCode
