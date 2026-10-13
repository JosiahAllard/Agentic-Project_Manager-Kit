# Bootstraps a repository onto the kit (docs/install.md; ported from the PM kit's new-repo.ps1).
#
#   powershell -NoProfile -File installer\new-repo.ps1 -Target <checkout> -OwnerEmail <owner>
#                                -AgentEmail <agents> [-MainBranch main] [-RepoName <name>]
#                                [-Stack python] [-PmData <folder> [-PrivateRemote <url>]] [-Stubs] [-Apply]
#
# The target is an existing git checkout (`git init -b main` first for a new one); the folder -Target
# names must be its top level. It writes these and nothing else (a repository holds no copy of the
# kit, and by default no stubs either: skills and roles run `<kit>\tools\<x>.py -C <checkout>`,
# decision 21 in docs/multi-host-plan.md section 7):
#   <Target>\.kit.json            "repo".main_branch, "repo".name, "owner".email, "owner".agent_emails,
#                                and with -Stack "gate": "tools/gate.json"
# "repo".name is the repository's short name: -RepoName, else the target folder's name lower-cased.
# Either must be a lower-case repo name ([a-z0-9][a-z0-9-]*); a folder name that is not one refuses,
# asking for -RepoName.
#   <Target>\<tools_dir>\<n>.py   with -Stubs only: the kit tool stubs, by `tools\kit_stubs.py write
#                                -C <Target>` (the old committed setup, for a repository that wants them)
#   <Target>\.gitignore, .gitattributes   templates\common's lines (.claude/worktrees/ and .codex/worktrees/
#                                keep the agents' worktrees out of the main checkout's status) plus the
#                                stack's gitignore/gitattributes lists. An existing file is never
#                                overwritten: only the lines it lacks are appended.
#   <Target>\docs\INDEX.md        a starter doc index (templates\common\INDEX.md), only if absent
#   <Target>\tools\gate.json      with -Stack: templates\stacks\<Stack>\tools\gate.json, only if absent
#   <Target>\tools\<file>        with -Stack: the stack's other files under templates\stacks\<Stack>\tools
#                                (dotnet: dotnet_each.py, godot: godot.cmd, python: js_tests.py), each only if absent; one already there is kept
#   <Target>\.claude\skills\...  with -Stack: the stack's skills, by `tools\stack_skill.py install` (Godot:
#                                godot-render-check), the kit's absolute path written over {kit_root};
#                                one already there is kept; a kit path it cannot write refuses
# It does not install the stack's CI workflows or its other files outside tools\ and .claude\skills\, templates\common\AGENTS.md and CLAUDE.md,
# the agent profiles, or .claude\settings.json (docs/install.md "What the script does not install").
# -OwnerEmail and -AgentEmail are required and must differ: an owner commit (Owner-Approved:) counts
# only when its author email is "owner".email, so an empty or shared identity would let an agent's
# commit pass as the owner's.
#
# Dry run by default: prints what it would write. -Apply writes. It never overwrites: a .kit.json or
# tools\gate.json already there, or (with -Stubs) a file where a stub goes that is not a stub, refuses
# before anything is written. A missing `origin` remote (new_worktree and preflight need it) is a warning
# that says what to run.
#
# -PmData <folder> (docs/pm-data-branch.md "Other users"): after the setup above, run the kit's
# `tools\pm_data.py -C <kit> init --path <folder> [--url <PrivateRemote>]`, which makes the PM-data
# branch of the KIT's repository (not the target's), its locked worktree at <folder>, and the
# private remote when -PrivateRemote is given. The dry run prints that command and runs nothing.
# Its result is reported as pm_data.py gives it: its lines, then its exit code as this script's.
#
# A write that fails (a read-only or locked file, a full disk) stops the script at once: it prints
# "FAILED", what was already written (and the target that failed, which may be partly written), and exits 1.
# It never prints "wrote" for a file it did not write. Every kit template (stack.json, gate.json,
# common\.gitignore, gitattributes, INDEX.md) and an existing .gitignore / .gitattributes is read at
# plan time: one that cannot be read, or (with -Stubs) a stub plan that cannot be read or a stack tools
# file named like a stub, refuses before any write.
#
# Exit: 0 dry run printed or files written, 1 refused or a write failed, 2 usage; with -PmData and -Apply, a non-zero
# exit of pm_data.py init (1 refused, 2 configuration, 3 made locally but the push failed, 4 stopped
# part-way, 5 crashed, 130 Ctrl+C) after the target's files were written.
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
$stubTool = Join-Path $kit 'tools\kit_stubs.py'
$utf8 = New-Object System.Text.UTF8Encoding($false)

function Fail([string]$msg, [int]$code = 1) { Write-Host "new-repo: REFUSED - $msg"; exit $code }

# Every write goes through Put-File: the script runs under Continue (see below), where a failed .NET
# call only prints an error and the script goes on to say "wrote". Here a failure stops it, with the
# files already written named so they can be undone.
$written = New-Object System.Collections.Generic.List[string]
# A file that was there already and only had lines appended is not the script's to delete: kept apart.
$appended = New-Object System.Collections.Generic.List[string]
function Stop-Write([string]$path, $err, [string]$verb = 'write') {
    Write-Host "new-repo: FAILED - cannot $verb $path - $($err.Exception.Message)"
    if ($written.Count) { Write-Host "  already written (delete them to undo): $($written -join ' , ')" }
    elseif (-not $appended.Count) { Write-Host "  nothing had been written before it" }
    foreach ($a in $appended) { Write-Host "  appended to $a : remove the '# Added by the kit installer' block to undo (the file is yours, do not delete it)" }
    # The target that failed is not in the list above: a write can fail part-way.
    if ($verb -eq 'write') { Write-Host "  $path may be partly written (the write that failed; delete it too if the script made it, check it if it was there before)" }
    exit 1
}
function Put-File([string]$path, [string]$text, [string]$how = 'wrote') {
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $path) -ErrorAction Stop | Out-Null
        if ($how -eq 'appended') { [IO.File]::AppendAllText($path, $text, $utf8) }
        else { [IO.File]::WriteAllText($path, $text, $utf8) }
    } catch { Stop-Write $path $_ }
    if ($how -eq 'appended') { $appended.Add($path) } else { $written.Add($path) }
}
function Copy-NewFile([string]$src, [string]$dst) {
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path $dst) -ErrorAction Stop | Out-Null
        [IO.File]::Copy($src, $dst, $false)
    } catch { Stop-Write $dst $_ }
    $written.Add($dst)
}

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
# Windows PowerShell 5.1 passes a native argument with a space inside plain double quotes, without
# escaping: an embedded `"` or a trailing `\` (read as `\"`) would split or join arguments. No
# path holds `"`, so one in a url is refused; a trailing `\` is trimmed (a drive root is kept).
if ($PrivateRemote.Contains('"') -or $PmData.Contains('"')) { Fail "-PmData and -PrivateRemote cannot contain a double quote" 2 }
function Trim-Slash([string]$p) {
    $t = $p.TrimEnd('\')
    if ($t -match '^[A-Za-z]:$') { return $t + '\' }
    if ($t) { return $t } else { return $p }
}
$PrivateRemote = Trim-Slash $PrivateRemote
# A relative path is taken from the PowerShell location, not the process folder (they differ once
# a session has used Set-Location).
function Full-Path([string]$p) {
    [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($p))
}
$pmTool = Join-Path $kit 'tools\pm_data.py'

$targetFull = (Full-Path $Target).TrimEnd('\')
$kitFull = [IO.Path]::GetFullPath($kit).TrimEnd('\') + '\'
if (($targetFull + '\').StartsWith($kitFull, [StringComparison]::OrdinalIgnoreCase)) {
    Fail "the target $targetFull is inside the kit itself"
}
if (-not (Test-Path -LiteralPath $targetFull -PathType Container)) { Fail "$targetFull is not a folder" }
# "repo".name: the repository's short name, checked with the lower-case grammar below.
# -cnotmatch: PowerShell's -notmatch ignores case.
$RepoName = $RepoName.Trim()
$nameRx = '^[a-z0-9][a-z0-9-]*$'
if ($RepoName) {
    if ($RepoName -cnotmatch $nameRx) { Fail "-RepoName '$RepoName' is not a lower-case repo name ([a-z0-9][a-z0-9-]*)" 2 }
} else {
    $RepoName = (Split-Path $targetFull -Leaf).ToLowerInvariant()
    if ($RepoName -cnotmatch $nameRx) {
        Fail "the folder name '$RepoName' is not a lower-case repo name ([a-z0-9][a-z0-9-]*); give -RepoName <the repository's short name>" 2
    }
}
# Native calls run under Continue: Windows PowerShell 5.1 turns a native stderr line into an error
# record, which Stop would throw on.
$ErrorActionPreference = 'Continue'
$top = & git -C $targetFull rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0 -or -not $top) { Fail "$targetFull is not a git checkout (git init -b $MainBranch first)" }
$topFull = [IO.Path]::GetFullPath(($top | Select-Object -First 1).Trim()).TrimEnd('\')
if ($topFull -ine $targetFull) { Fail "$targetFull is inside the checkout $topFull; name its top level" }
if ($Stubs -and -not (Test-Path -LiteralPath $stubTool)) { Fail "the kit at $kit has no tools\kit_stubs.py" 2 }
# Every kit tool this script runs is a python script: with no python on PATH a call would only print an
# error and the plan would look empty. Refuse here, before anything is written.
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail "python is not on PATH; the kit's tools need it (nothing was written)" 2 }
# The PM-data command: argv for the call, and the same quoted for the printed line.
$pmArgs = @()
$pmLine = ''
if ($PmData) {
    if (-not (Test-Path -LiteralPath $pmTool)) { Fail "the kit at $kit has no tools\pm_data.py" 2 }
    $pmPath = Trim-Slash (Full-Path $PmData)
    $pmArgs = @($pmTool, '-C', $kitFull.TrimEnd('\'), 'init', '--path', $pmPath)
    if ($PrivateRemote) { $pmArgs += @('--url', $PrivateRemote) }
    # The printed command pastes into PowerShell: single quotes (a `'` doubled) around any argument
    # that is not plain.
    $pmLine = 'python ' + (($pmArgs | ForEach-Object {
        if ($_ -match '^[A-Za-z0-9_./:\\-]+$') { $_ } else { "'" + ($_ -replace "'", "''") + "'" }
    }) -join ' ')
}

$kitJson = Join-Path $targetFull '.kit.json'
if (Test-Path -LiteralPath $kitJson) { Fail "$kitJson exists already; nothing was written" }

# The stack, if one was named: its templates\stacks\<Stack> folder holds the gate section and the
# gitignore/gitattributes lists.
$stackLines = @{ gitignore = @(); gitattributes = @() }
$gateSrc = $null
$toolFiles = @()
$gateDst = Join-Path $targetFull 'tools\gate.json'
if ($Stack) {
    if ($Stack -notmatch '^[a-z0-9]+$') { Fail "-Stack '$Stack' is not a stack name" 2 }
    $stackDir = Join-Path $kit "templates\stacks\$Stack"
    if (-not (Test-Path -LiteralPath $stackDir -PathType Container)) {
        $known = (Get-ChildItem -LiteralPath (Join-Path $kit 'templates\stacks') -Directory | ForEach-Object { $_.Name }) -join ', '
        Fail "-Stack '$Stack' is not a stack the kit has ($known)" 2
    }
    # Every kit template is read here, at plan time, under Stop (the script runs under Continue, where a
    # failed read only prints an error and goes on): an unreadable one refuses before anything is written.
    $stackJson = Join-Path $stackDir 'stack.json'
    try { $meta = Get-Content -LiteralPath $stackJson -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop }
    catch { Fail "cannot read $stackJson - $($_.Exception.Message); nothing was written" }
    foreach ($k in 'gitignore', 'gitattributes') { if ($meta.$k) { $stackLines[$k] = @($meta.$k) } }
    $gateSrc = Join-Path $stackDir 'tools\gate.json'
    if (-not (Test-Path -LiteralPath $gateSrc)) { Fail "the kit has no $gateSrc" 2 }
    try { $gateText = [IO.File]::ReadAllText($gateSrc, $utf8) }
    catch { Fail "cannot read $gateSrc - $($_.Exception.Message); nothing was written" }
    # The stack's other tools files (dotnet_each.py, godot.cmd, js_tests.py, by stack); gate.json is handled apart (it must exist before
    # the stubs are written, and one already there refuses).
    $toolFiles = @(Get-ChildItem -LiteralPath (Join-Path $stackDir 'tools') -File | Where-Object { $_.Name -ine 'gate.json' } |
        ForEach-Object { [pscustomobject]@{ Name = $_.Name; Src = $_.FullName; Dst = (Join-Path $targetFull "tools\$($_.Name)") } })
    if (Test-Path -LiteralPath $gateDst) { Fail "$gateDst exists already; nothing was written" }
}
$gateLine = if ($Stack) { "  `"gate`": `"tools/gate.json`",`n" } else { '' }
$text = "{`n" +
        "  `"repo`": {`"main_branch`": `"$MainBranch`", `"name`": `"$RepoName`"},`n" +
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
    $tplPath = Join-Path $kit "templates\common\$tplName"
    try { $tpl = @(Get-Content -LiteralPath $tplPath -Encoding UTF8 -ErrorAction Stop) }
    catch { Fail "cannot read $tplPath - $($_.Exception.Message); nothing was written" }
    $stackWant = @($stackWant | Where-Object { $_ -and $_.Trim() -and -not $_.TrimStart().StartsWith('#') })
    $want = @($tpl | Where-Object { $_.Trim() -and -not $_.TrimStart().StartsWith('#') }) + @($stackWant)
    $new = -not (Test-Path -LiteralPath $path)
    $have = @()
    # Under Continue a failed read only prints an error and leaves $have empty: read it under Stop and
    # refuse here, before anything is written.
    if (-not $new) {
        try { $have = @(Get-Content -LiteralPath $path -Encoding UTF8 -ErrorAction Stop | ForEach-Object { Norm $_ }) }
        catch { Fail "cannot read $path - $($_.Exception.Message); nothing was written" }
    }
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
# Advisory: the tracked .md files the starter index does not name (the doc index check wants every
# one named on an entry line). Only when the starter is what gets written; never a refusal.
$unindexed = @()
if ($indexNew) {
    try { $indexText = [IO.File]::ReadAllText($indexSrc, $utf8) }
    catch { Fail "cannot read $indexSrc - $($_.Exception.Message); nothing was written" }
    $named = @([regex]::Matches($indexText, '`([^`]+)`') | ForEach-Object { $_.Groups[1].Value })
    # -z with core.quotePath=false gives the names raw (git otherwise quotes a non-ASCII name as "\303\251"),
    # NUL-separated and UTF-8; read the bytes as UTF-8 through a process, not the console's code page.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = "-C `"$targetFull`" -c core.quotePath=false ls-files -z -- `"*.md`""
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = $utf8
    $lsOut = ''
    try {
        $proc = [System.Diagnostics.Process]::Start($psi)
        $lsOut = $proc.StandardOutput.ReadToEnd()
        $null = $proc.StandardError.ReadToEnd()
        $proc.WaitForExit()
    } catch { $lsOut = '' }
    $unindexed = @($lsOut.Split([char]0) | Where-Object { $_ -and ($named -notcontains $_) })
}

# With -Stubs, the stubs the kit would write, from kit_stubs itself (it reads the target's .kit.json,
# which is not there yet: the default tools_dir and tool list apply, as they will after -Apply).
$plan = @()
$planCode = 0
if ($Stubs) {
    $plan = & python $stubTool write -C $targetFull --dry-run 2>&1
    $planCode = $LASTEXITCODE
}

# The default remote name: .kit.json "repo".remote is not set by this script.
$skillTool = Join-Path $kit 'tools\stack_skill.py'
$skillPlan = @()
if ($Stack) {
    if (-not (Test-Path -LiteralPath $skillTool)) { Fail "the kit at $kit has no tools\stack_skill.py" 2 }
    $skillPlan = @(& python $skillTool install --stack $Stack -C $targetFull 2>&1 | ForEach-Object { "$_" })
    if ($LASTEXITCODE -ne 0) { Fail "stack_skill.py refused the $Stack stack's skills ($($skillPlan -join ' ')); nothing was written" }
}
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
if ($Stubs) { foreach ($line in @($plan)) { Write-Host "  $line" } }
else { Write-Host "  none (skills and roles run $($kitFull.TrimEnd('\'))\tools\<x>.py -C <checkout>; -Stubs writes them)" }
Write-Host ""
Write-Host "other files:"
foreach ($f in $lineFiles) {
    if ($f.New) { Write-Host "  would create  $($f.Path)" }
    elseif ($f.Missing.Count) { Write-Host "  would append  $($f.Path)   ($($f.Missing.Count) missing: $($f.Missing -join ' , ')); the lines it has are kept" }
    else { Write-Host "  unchanged     $($f.Path)   (has every line already)" }
}
if ($indexNew) { Write-Host "  would create  $indexDst   (starter doc index)" } else { Write-Host "  kept          $indexDst   (exists)" }
if ($unindexed.Count) {
    Write-Host "  add these to docs/INDEX.md ($($unindexed.Count) tracked .md the starter index does not name; the doc index check lists them until you do):"
    foreach ($u in $unindexed) { Write-Host "    $u" }
}
if ($Stack) { Write-Host "  would create  $gateDst   (from templates\stacks\$Stack\tools\gate.json)" }
else { Write-Host "  no gate section (no -Stack): see Next, step 3" }
foreach ($t in $toolFiles) {
    if (Test-Path -LiteralPath $t.Dst) { Write-Host "  kept          $($t.Dst)   (exists)" }
    else { Write-Host "  would create  $($t.Dst)   (from templates\stacks\$Stack\tools\$($t.Name))" }
}
foreach ($l in $skillPlan) { Write-Host "  $l   (stack skill, kit path written in)" }
Write-Host ""
if ($hasOrigin) { Write-Host "remote:   origin -> $(@($remoteUrl)[0])" }
else {
    Write-Host "WARNING:  no remote named origin. new_worktree.py and preflight.py --fetch need it to fetch from."
    Write-Host "          git -C `"$targetFull`" remote add origin <url>   then   git -C `"$targetFull`" push -u origin $MainBranch"
}
if ($PmData) {
    Write-Host ""
    Write-Host "PM data (the kit's own repository, docs/pm-data-branch.md):"
    $when = if ($Apply) { 'runs' } else { 'would run' }
    Write-Host "  $when after the files above:  $pmLine"
}
if ($planCode -ne 0) { Fail "kit_stubs.py refused the stubs above (exit $planCode); nothing was written" }
# A stack tools file that has a stub's name would be copied first and then be a file "that is not a
# stub" where the stub goes. Refuse now, in the dry run too.
$stubNames = @(@($plan) | ForEach-Object { if ("$_" -match '[\\/]([^\\/]+\.py)\s*$') { $Matches[1] } })
if (@($plan | Where-Object { "$_".Trim() }).Count -and -not $stubNames.Count) {
    Fail "cannot read the stub plan (kit_stubs.py printed lines with no stub file name in them, shown above); nothing was written"
}
foreach ($t in $toolFiles) {
    if (@($stubNames | Where-Object { $_ -ieq $t.Name }).Count) {
        Fail "the $Stack stack's tools file $($t.Name) has the name of a kit tool stub; nothing was written"
    }
}
if (-not $Apply) { exit 0 }

# The stack's skills go before anything else, with the kit's absolute path written in; a refusal
# stops here, before .kit.json (the plan-time run above makes one unlikely).
$skillWritten = New-Object System.Collections.Generic.List[string]
if ($Stack) {
    $skillOut = @(& python $skillTool install --stack $Stack -C $targetFull --apply 2>&1 | ForEach-Object { "$_" })
    foreach ($l in $skillOut) {
        Write-Host "  $l"
        if ($l -match '^wrote\s+(.+)$') { $written.Add($Matches[1].Trim()); $skillWritten.Add($Matches[1].Trim()) }
    }
    if ($LASTEXITCODE -ne 0) {
        Write-Host "new-repo: STOPPED - stack_skill.py exited $LASTEXITCODE; .kit.json was not written."
        if ($written.Count) { Write-Host "  already written (delete them to undo): $($written -join ' , ')" }
        exit 1
    }
}
# The gate file goes first: kit_stubs reads .kit.json and refuses a "gate" path that is not there.
if ($Stack) {
    Put-File $gateDst $gateText
    Write-Host "  wrote    $gateDst"
    foreach ($t in $toolFiles) {
        if (Test-Path -LiteralPath $t.Dst) { continue }
        Copy-NewFile $t.Src $t.Dst
        Write-Host "  wrote    $($t.Dst)"
    }
}
try {
    $fs = New-Object IO.FileStream($kitJson, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    try { $bytes = $utf8.GetBytes($text); $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
} catch {
    $did = if ($written.Count) { "only $($written -join ' , ') was written (delete to undo)" } else { "nothing was written" }
    Fail "cannot create $kitJson - $($_.Exception.Message); $did"
}
$written.Add($kitJson)
if ($Stubs) {
    $out = & python $stubTool write -C $targetFull 2>&1
    $code = $LASTEXITCODE
    foreach ($line in @($out)) { Write-Host "  $line" }
    if ($code -ne 0) {
        $wrote = if ($Stack) { (@(".kit.json", $gateDst) + @($toolFiles | Where-Object { $written -contains $_.Dst } | ForEach-Object { $_.Dst }) + @($skillWritten)) -join ', ' } else { ".kit.json" }
        Write-Host "new-repo: STOPPED - kit_stubs.py write exited $code after $wrote were written, and any stubs listed above."
        Write-Host "  The .gitignore, .gitattributes and docs/INDEX.md were not touched. To undo: delete $wrote and the stubs listed, then run again."
        exit 1
    }
    $written.Add('(and the stubs listed above)')
}
foreach ($f in $lineFiles) {
    if (-not $f.Body.Count) { continue }
    $chunk = ($f.Body -join "`n") + "`n"
    if ($f.New) { Put-File $f.Path $chunk; Write-Host "  wrote    $($f.Path)" }
    else {
        try { $old = [IO.File]::ReadAllText($f.Path) } catch { Stop-Write $f.Path $_ 'read' }
        # Append in the file's own line ending (CRLF when it has any).
        $nl = if ($old.Contains("`r`n")) { "`r`n" } else { "`n" }
        $chunk = ($f.Body -join $nl) + $nl
        $sep = if ($old.Length -and -not $old.EndsWith("`n")) { $nl } else { '' }
        Put-File $f.Path ($sep + $chunk) 'appended'
        Write-Host "  appended $($f.Path)   ($($f.Missing.Count) line(s))"
    }
}
if ($indexNew) {
    Put-File $indexDst $indexText
    Write-Host "  wrote    $indexDst"
}
Write-Host ""
$what = if ($Stubs) { '.kit.json, the stubs and the starter files' } else { '.kit.json and the starter files' }
Write-Host "new-repo: wrote $what in $targetFull"
$pmCode = 0
if ($PmData) {
    Write-Host ""
    Write-Host "PM data: $pmLine"
    # Each line is shown as it comes (so a Ctrl+C that ends this script leaves what was made on
    # screen) and kept for the checks below.
    $pmOut = @(& python @pmArgs 2>&1 | ForEach-Object { $l = "$_"; Write-Host "  $l"; $l })
    $pmCode = $LASTEXITCODE
    if ($pmCode -eq 0) { Write-Host "PM data: pm_data.py init exited 0 (done)" }
    else {
        $why = @{ 1 = 'refused, it changed nothing'; 2 = 'usage or a configuration it cannot read';
                  3 = 'made locally, the push to the private remote failed; its lines above say how to recover';
                  4 = 'stopped part-way; run it again with --adopt to finish';
                  5 = 'it crashed; what it made before the error is not known, run it again with --adopt to check';
                  130 = 'interrupted (Ctrl+C); the lines above are what it made' }[$pmCode]
        if (-not $why) { $why = 'an exit code pm_data.py does not document' }
        # Fallback for a pm_data.py that still lets a Python error escape (exit 1, a traceback):
        # never call that a refusal that changed nothing.
        if (@($pmOut | Where-Object { "$_" -match '^Traceback \(most recent call last\):' }).Count) {
            $why = 'it crashed; what it made before the error is not known, check the kit repository'
        }
        Write-Host "new-repo: STOPPED - the files above were written in $targetFull; pm_data.py init exited $pmCode ($why)."
        Write-Host "  Fix what its lines name, then run the PM-data step alone:  $pmLine"
    }
}
Write-Host "Next:"
$asUsual = @('.gitattributes', 'docs/INDEX.md') + @($toolFiles | ForEach-Object { "tools/$($_.Name)" })
$asUsual = ($asUsual[0..($asUsual.Count - 2)] -join ', ') + ' and ' + $asUsual[-1]
Write-Host "  1. review .kit.json (docs/kit-json.md names every key). Commit .kit.json and tools/gate.json under the"
$theStubs = if ($Stubs) { 'the stubs, ' } else { '' }
Write-Host "     owner's identity (owner-only paths: the kit's tools\commit.py refuses an agent's); commit $theStubs.gitignore,"
Write-Host "     $asUsual as usual"
if ($Stubs) { Write-Host "  2. python `"$stubTool`" check -C `"$targetFull`"   (exit 0: every stub current)" }
else { Write-Host "  2. agents run the kit's tools as python `"$kit\tools\<x>.py`" -C <checkout>; nothing to commit for them" }
if ($Stack) {
    Write-Host "  3. tools/gate.json is the $Stack gate: edit its globs and test command to the project."
    $passNote = @{
        python = "its pass check needs a line 'Ran N tests' with N of 1 or more, so keep at least one test or the step fails"
        godot  = "its test step passes only on a line 'UNIT TESTS OK', so the test scene must print it (and only when every test passed); set GODOT_DIR for tools/godot.cmd"
    }[$Stack]
    if (-not $passNote) { $passNote = "its steps pass on their exit status and fail patterns (see the steps in tools/gate.json)" }
    Write-Host "     $passNote"
} else {
    Write-Host "  3. the gate: with no gate section gate.py detects the stack (source: auto; docs/gate.md). To choose the steps, copy a stack's"
    Write-Host "     $kit\templates\stacks\<stack>\tools\gate.json to tools\gate.json and add  `"gate`": `"tools/gate.json`"  to .kit.json,"
    Write-Host "     or write gate.steps in .kit.json (docs/kit-json.md)"
}
Write-Host "  4. git -C `"$targetFull`" config core.hooksPath `"$kit\githooks`"   (the kit's pre-push hook)"
if (-not $hasOrigin) {
    Write-Host "  5. the remote: git -C `"$targetFull`" remote add origin <url>, then push $MainBranch (no origin yet)"
}
exit $pmCode
