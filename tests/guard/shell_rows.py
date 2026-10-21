"""More rows for tests/guard/test_rules.py: shell keywords, lines the guard cannot read, git
option spellings, shell writes, Start-Process, nested shells, root-search spellings and the false
denies that must stay allowed (docs/guard.md "Rules", "Fallback"). Same shape as ROWS there.

Backslashes are built from BS so no editor or shell on this machine can rewrite them."""
from . import world as W
from .world import bash, ps

BS = chr(92)
LOCK = 'python "{kit}/tools/machine_lock.py" run'


def win(*parts):
    return BS.join(parts)


def heavy(slug, rest, kind='engine-run'):
    return LOCK + ' --kind %s --repo demo --slug %s -- %s' % (kind, slug, rest)


ROWS = [
    # ---------------------------------------------------------------- shell keywords
    ('if/then hiding a checkout in main', lambda w: bash('if true; then git checkout main; fi', w.main), 'deny', 'git-checkout', None),
    ('for/do running godot', lambda w: bash('for f in a; do godot --headless; done', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('while/do running godot', lambda w: bash('while true; do godot --headless; done', w.wt), 'deny', 'heavy', None),
    ('! before a stash in main', lambda w: bash('! git stash', w.main), 'deny', 'git-stash', None),
    ('time before a stash in main', lambda w: bash('time git stash', w.main), 'deny', 'git-stash', None),
    ('else block checkout in main, PowerShell', lambda w: ps('if ($x) { git status } else { git checkout main }', w.main),
     'deny', 'git-checkout', None),
    ('cmd if hiding godot', lambda w: ps('cmd /c "if exist x godot --headless"', w.wt), 'deny', 'heavy', None),

    # ---------------------------------------------------------------- lines the guard cannot read
    ('subagent variable program in its worktree', lambda w: bash('$TOOL --version', w.wt, sub=True), 'deny', 'unreadable',
     'bash "{temp}/script.sh"'),
    ('main-role variable program in its worktree', lambda w: bash('$TOOL --version', w.wt), None, None, None),
    ('main-role variable program in main', lambda w: bash('"$TOOL" --version', w.main), 'deny', 'unreadable',
     'bash "{temp}/script.sh"'),
    ('PowerShell assignment is no program', lambda w: ps('$p = "a"; Write-Output $p', w.main, sub=True), None, None, None),
    ('PowerShell expression statement', lambda w: ps('$x.Count', w.main, sub=True), None, None, None),
    ('PowerShell assignment from git status in main', lambda w: ps('$s = git status --short', w.main, sub=True), None, None, None),
    ('PowerShell assignment from a checkout in main', lambda w: ps('$s = git checkout main', w.main), 'deny', 'git-checkout', None),
    ('PowerShell call of a variable in main', lambda w: ps('& $exe --headless', w.main), 'deny', 'unreadable', None),

    # ---------------------------------------------------------------- words the tokenizer cannot read cleanly
    ('backslash inside a program word', lambda w: bash('g' + BS + 'odot --headless', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('ANSI-C hex escape hiding git in main', lambda w: bash("$'" + BS + "x67it' checkout main", w.main), 'deny', 'git-checkout', None),
    ('ANSI-C octal escape hiding git, subagent in its worktree', lambda w: bash("$'" + BS + "147it' status", w.wt, sub=True),
     'deny', 'unreadable', None),
    ('substitution inside ${...}', lambda w: bash('echo ${x:-$(godot --headless)}', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('brace expansion as the program', lambda w: bash('{godot,x} --headless', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot x --headless')),
    ('brace expansion past 256 words', lambda w: bash('rm -f x/{%s}.txt; ls' % ','.join(str(n) for n in range(257)),
                                                      w.wt), 'deny', 'brace-limit',
     'rm -f <its names, written out or in brace words of at most 256 each>; ls'),
    ('brace expansion past 256 words in a nested bash, subagent',
     lambda w: ps("bash -c 'rm x/{%s}'" % ','.join(str(n) for n in range(257)), w.wt, sub=True), 'deny', 'brace-limit',
     'rm <its names, written out or in brace words of at most 256 each>'),

    # ---------------------------------------------------------------- git spellings
    ('GIT_DIR prefix hiding a checkout', lambda w: bash('GIT_DIR=%s/.git git checkout main' % W.fwd(w.main), w.wt), 'deny', 'git-checkout', None),
    ('GIT_WORK_TREE export then stash', lambda w: bash('export GIT_WORK_TREE=%s; git stash' % W.fwd(w.main), w.wt), 'deny', 'git-stash', None),
    ('PowerShell env GIT_DIR then reset --hard', lambda w: ps('$env:GIT_DIR = "%s"; git reset --hard' % win(w.main, '.git'), w.wt),
     'deny', 'git-reset-hard', None),
    ('git -c alias', lambda w: bash('git -c alias.co=checkout co main', w.wt), 'deny', 'git-alias', 'git checkout main'),
    ('git -c alias running a shell', lambda w: bash("git -c 'alias.x=!godot --headless' x", w.wt), 'deny', 'git-alias', None),
    ('git -calias attached', lambda w: bash('git -calias.st=stash st', w.wt), 'deny', 'git-alias', 'git stash'),
    ('reset --ha in main', lambda w: bash('git reset --ha origin/main', w.main), 'deny', 'git-reset-hard',
     'git -C "{main}" reset --keep origin/main'),
    ('commit --al in main', lambda w: bash('git commit --al -m x', w.main), 'deny', 'git-commit-all', None),
    ('push --no-veri to main', lambda w: bash('git push --no-veri origin gated:main', w.wt), 'deny', 'git-push-main', None),
    ('push --forc to main', lambda w: bash('git push --forc origin gated:main', w.wt), 'deny', 'git-push-main', None),
    ('restore --sta in main', lambda w: bash('git restore --sta a.py', w.main), None, None, None),
    ('clean --dry in main', lambda w: bash('git clean --dry -d', w.main), None, None, None),
    ('branch --del --forc', lambda w: bash('git branch --del --forc kit/old', w.wt), 'deny', 'git-branch-force-delete', None),
    ('subagent apply in main', lambda w: bash('git apply x.patch', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent apply --check in main', lambda w: bash('git apply --check x.patch', w.main, sub=True), None, None, None),
    ('subagent pull in main', lambda w: ps('git pull', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent pull in its worktree', lambda w: bash('git pull', w.wt, sub=True), None, None, None),

    # ---------------------------------------------------------------- shell writes, a subagent
    ('sed -ni into main', lambda w: bash("sed -ni 's/a/b/' %s/x.txt" % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout',
     "sed -ni 's/a/b/' \"{wt}/x.txt\""),
    ('sed -e reading main', lambda w: bash("sed -e 's/i/x/' %s/x.txt" % W.fwd(w.main), w.wt, sub=True), None, None, None),
    ('sed -n reading main', lambda w: bash('sed -n 1p %s/x.txt' % W.fwd(w.main), w.wt, sub=True), None, None, None),
    ('perl -pi into main', lambda w: bash("perl -pi -e 's/a/b/' %s/x.txt" % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('perl -i -pe into main', lambda w: bash("perl -i -pe 's/a/b/' %s/x.txt" % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('perl -ne reading main', lambda w: bash("perl -ne 'print' %s/x.txt" % W.fwd(w.main), w.wt, sub=True), None, None, None),
    ('dd of= into main', lambda w: bash('dd if=/dev/zero of=%s/x.bin bs=1 count=1' % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('curl -sSLo into main', lambda w: bash('curl -sSLo %s/x.zip https://example.invalid/x.zip' % W.fwd(w.main), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('curl --output= into main', lambda w: bash('curl --output=%s/x.zip https://example.invalid/x.zip' % W.fwd(w.main), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('curl to stdout in main', lambda w: bash('curl -s https://example.invalid/x', w.main, sub=True), None, None, None),
    ('curl -O in main', lambda w: bash('curl -O https://example.invalid/x.zip', w.main, sub=True), 'deny', 'main-checkout', None),
    ('wget in main', lambda w: bash('wget https://example.invalid/x.zip', w.main, sub=True), 'deny', 'main-checkout', None),
    ('iwr -OutFile into main', lambda w: ps('iwr https://example.invalid/x -OutFile %s' % win(w.main, 'x.zip'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Invoke-WebRequest -OutF into main', lambda w: ps('Invoke-WebRequest -Uri https://example.invalid/x -OutF %s' % win(w.main, 'x.zip'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Expand-Archive -Dest into main', lambda w: ps('Expand-Archive a.zip -Dest %s' % win(w.main, 'd'), w.wt, sub=True), 'deny', 'main-checkout',
     'Expand-Archive a.zip -Dest "{wt}/d"'),
    ('Export-Csv into main', lambda w: ps('Get-Process | Export-Csv %s' % win(w.main, 'p.csv'), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('patch in main', lambda w: bash('patch -p1 < x.diff', w.main, sub=True), 'deny', 'main-checkout', None),
    ('patch -d main from a worktree', lambda w: bash('patch -d %s -p1 -i x.diff' % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('rsync into main', lambda w: bash('rsync -a src/ %s/dst/' % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('rsync out of main', lambda w: bash('rsync -a %s/src/ out/' % W.fwd(w.main), w.wt, sub=True), None, None, None),
    ('python -c in main', lambda w: bash('python -c "open(\'x\', \'w\').write(\'y\')"', w.main, sub=True), 'deny', 'unplaced', None),
    ('python -c in its worktree', lambda w: bash('python -c "open(\'x\', \'w\').write(\'y\')"', w.wt, sub=True), None, None, None),
    ('python heredoc in main', lambda w: bash("python - <<'EOF'\nprint(1)\nEOF", w.main, sub=True), 'deny', 'unplaced', None),
    ('IO.File write in main', lambda w: ps("[IO.File]::WriteAllText('x.txt', 'y')", w.main, sub=True), 'deny', 'main-checkout', None),
    ('System.IO.File append in main', lambda w: ps("[System.IO.File]::AppendAllText('x.txt', 'y')", w.main, sub=True), 'deny', 'main-checkout', None),
    ('IO.File read in main', lambda w: ps("[IO.File]::ReadAllText('x.txt')", w.main, sub=True), None, None, None),
    ('Copy-Item -Dest into main', lambda w: ps('Copy-Item a.txt -Dest %s' % win(w.main, 'b.txt'), w.wt, sub=True), 'deny', 'main-checkout',
     'Copy-Item a.txt -Dest "{wt}/b.txt"'),
    ('Set-Content -Pa into main', lambda w: ps('Set-Content -Pa %s -Va 1' % win(w.main, 'x.txt'), w.wt, sub=True), 'deny', 'main-checkout', None),

    # ---------------------------------------------------------------- Start-Process and start
    ('Start-Process -Wait before the program', lambda w: ps('Start-Process -Wait godot.exe -ArgumentList --headless', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot.exe --headless')),
    ('Start-Process blender with dashed arguments', lambda w: ps("Start-Process -NoNewWindow -FilePath blender -ArgumentList '-b', 'x.blend'", w.wt),
     'deny', 'heavy', heavy('wt', 'blender -b x.blend', 'render')),
    ('Start-Process redirect into main', lambda w: ps('Start-Process python -ArgumentList x.py -RedirectStandardOutput %s' % win(w.main, 'o.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    # a shell Start-Process starts runs its -ArgumentList as a nested line: the elements joined with
    # spaces, as Start-Process passes them (commands._sp_shell_text)
    ('Start-Process cmd /c redirect into main, double-quoted list', lambda w: ps(
        'Start-Process cmd -ArgumentList "/c echo x > %s"' % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('saps cmd /c redirect into main, list as an array', lambda w: ps(
        "saps cmd -ArgumentList '/c','echo','x','>','%s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process -FilePath cmd.exe /c redirect into main, split list', lambda w: ps(
        "Start-Process -FilePath cmd.exe -ArgumentList '/c', 'echo x > %s' -Wait" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd /c redirect into main, single-quoted list', lambda w: ps(
        "Start-Process cmd -ArgumentList '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd /c quoted command redirecting into main', lambda w: ps(
        "Start-Process cmd -ArgumentList '/c \"echo x > %s\"'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd in main as its working directory', lambda w: ps(
        "Start-Process cmd -WorkingDirectory %s -ArgumentList '/c echo x > f.txt'" % w.main, w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd with a computed argument list', lambda w: ps('Start-Process cmd -ArgumentList $a', w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd with a /c the guard cannot read', lambda w: ps(
        "Start-Process cmd -ArgumentList '/q/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd /c echo stays allowed', lambda w: ps("Start-Process cmd -ArgumentList '/c echo hi'", w.wt, sub=True),
     None, None, None),
    ('Start-Process cmd /c redirect in its worktree stays allowed', lambda w: ps(
        "Start-Process cmd -ArgumentList '/d', '/c', 'echo hi > out.txt' -Wait", w.wt, sub=True), None, None, None),
    # cmd splits its switches at , ; = too and finds a /c inside a word: any word before the run
    # switch that holds /c (/k, /r) is unreadable (commands.CMD_RUN_HIDDEN_RE)
    ('Start-Process cmd ;/c into main', lambda w: ps(
        "Start-Process cmd -ArgumentList ';/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd ,/c into main', lambda w: ps(
        "Start-Process cmd -ArgumentList ',/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd =/C into main', lambda w: ps(
        "Start-Process cmd -ArgumentList '=/C echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd x/c into main', lambda w: ps(
        "Start-Process cmd -ArgumentList 'x/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd x"/c" into main', lambda w: ps(
        "Start-Process cmd -ArgumentList 'x\"/c\" echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd ;/c into main', lambda w: ps("cmd ';/c' 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd ,/c into main', lambda w: ps("cmd ',/c' 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd =/k into main', lambda w: ps("cmd =/k 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd x/c into main', lambda w: ps("cmd x/c 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd x"/c" into main', lambda w: ps("cmd 'x\"/c\"' 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd ;//c into main, Git Bash', lambda w: bash("cmd ';//c' 'echo x > %s/f.txt'" % W.fwd(w.main), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('cmd with a computed word before its switch', lambda w: ps(
        "cmd $a 'echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True), 'deny', 'unreadable', None),
    ('cmd /d /c echo stays allowed', lambda w: ps("cmd /d /c 'echo hi > out.txt'", w.wt, sub=True), None, None, None),
    # bash and powershell Start-Process starts run at its -WorkingDirectory too
    ('Start-Process powershell in main as its working directory', lambda w: ps(
        "Start-Process powershell -WorkingDirectory %s -ArgumentList '-Command', 'echo x > f.txt'" % w.main,
        w.wt, sub=True), 'deny', 'main-checkout', None),
    ('Start-Process bash in main as its working directory', lambda w: ps(
        "Start-Process bash -WorkingDirectory %s -ArgumentList '-c', '\"echo x > f.txt\"'" % w.main, w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process bash in its worktree stays allowed', lambda w: ps(
        "Start-Process bash -WorkingDirectory %s -ArgumentList '-c', '\"echo x > f.txt\"'" % w.wt, w.wt, sub=True),
     None, None, None),
    ('Start-Process powershell in a computed working directory', lambda w: ps(
        "Start-Process powershell -WorkingDirectory $d -ArgumentList '-Command', 'echo x > f.txt'", w.wt,
        sub=True), 'deny', 'unplaced', None),
    ('Start-Process powershell reading in a computed working directory stays allowed', lambda w: ps(
        "Start-Process powershell -WorkingDirectory $d -ArgumentList '-Command', 'echo hi'", w.wt, sub=True),
     None, None, None),
    ('start cmd /c redirect into main', lambda w: ps(
        "start cmd -ArgumentList '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    # the words carry no commas: an array is several words, every one of them in the list, and a
    # program given by position after -ArgumentList is the last word it took
    ('Start-Process cmd with a positional array into main', lambda w: ps(
        "Start-Process cmd '/c','echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd -ArgumentList: with an array into main', lambda w: ps(
        "Start-Process cmd -ArgumentList:'/c','echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process with cmd after its list into main', lambda w: ps(
        "Start-Process -ArgumentList '/c echo x > %s' cmd" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process with cmd after an array into main', lambda w: ps(
        "Start-Process -ArgumentList '/c','echo x > %s' cmd" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process with cmd after its list in its worktree stays allowed', lambda w: ps(
        "Start-Process -ArgumentList '/c echo x > f.txt' cmd", w.wt, sub=True), None, None, None),
    # the common parameters (-Verbose, -ErrorAction/-ea ...) and -PSPath are parameters too: a list
    # ends at them and none is the program; any other parameter word makes the call unreadable
    ('Start-Process with cmd after its list then -Verbose into main', lambda w: ps(
        "Start-Process -ArgumentList '/c echo x > %s' cmd -Verbose" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process with cmd after its list then -ErrorAction into main', lambda w: ps(
        "Start-Process -ArgumentList '/c echo x > %s' cmd -ErrorAction Stop" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process -ErrorAction before cmd into main', lambda w: ps(
        "Start-Process -ErrorAction Stop cmd '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process -Verbose before cmd into main', lambda w: ps(
        "Start-Process -Verbose cmd -ArgumentList '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('saps -ea before cmd with an array into main', lambda w: ps(
        "saps -ea 0 cmd '/c','echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process -PSPath cmd into main', lambda w: ps(
        "Start-Process -PSPath cmd -ArgumentList '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process -FilePath: then cmd into main', lambda w: ps(
        "Start-Process -FilePath: cmd -ArgumentList '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd -WorkingDirectory: then main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory: %s -ArgumentList '/c echo x > f.txt'" % w.main, w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd -WorkingDirectory: then its worktree stays allowed', lambda w: ps(
        "Start-Process cmd -WorkingDirectory: %s -ArgumentList '/c echo x > f.txt'" % w.wt, w.wt, sub=True),
     None, None, None),
    # a -WorkingDirectory is a directory only when its text names one place exactly: a bare drive
    # (that drive's current location), a wildcard (Start-Process resolves the text as a pattern) or
    # a drive-relative C:x leaves the nested line with no directory, so a write there is refused
    # (commands.sp_workdir_unplaceable). A subagent's write jail refused these before, judged at the
    # wrong place; tests/guard/test_start_process_workdir.py holds the roles the jail does not cover
    ('Start-Process cmd in a bare drive from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory %s -ArgumentList '/c','echo x > f.txt'" % w.main[:2], w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd -WorkingDirectory: a bare drive from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory:%s '/c','echo x > f.txt'" % w.main[:2], w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd in a ? wildcard directory from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory '%s?' -ArgumentList '/c echo x > f.txt'" % w.main[:-1], w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd in a * wildcard directory from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory '%s*' -ArgumentList '/c echo x > f.txt'" % w.main[:-1], w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd in a [ wildcard directory from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory '%s[r]epo' -ArgumentList '/c echo x > f.txt'" % w.main[:-4], w.main,
        sub=True), 'deny', 'unplaced', None),
    ('Start-Process powershell in a ? wildcard directory from main', lambda w: ps(
        "Start-Process powershell -WorkingDirectory '%s?' -ArgumentList '-Command', 'echo x > f.txt'" % w.main[:-1],
        w.main, sub=True), 'deny', 'unplaced', None),
    ('Start-Process cmd in a drive-relative directory from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory %srepo -ArgumentList '/c echo x > f.txt'" % w.main[:2], w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd in a relative wildcard directory from main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory '..%srep?' -ArgumentList '/c echo x > f.txt'" % BS, w.main, sub=True),
     'deny', 'unplaced', None),
    ('Start-Process cmd in a relative directory that is main', lambda w: ps(
        "Start-Process cmd -WorkingDirectory %s -ArgumentList '/c echo x > f.txt'" % win('..', 'repo'), w.main, sub=True),
     'deny', 'main-checkout', None),
    ('Start-Process cmd in a relative directory from main into its worktree stays allowed', lambda w: ps(
        "Start-Process cmd -WorkingDirectory %s -ArgumentList '/c echo x > f.txt'" % win('.claude', 'worktrees', 'wt'),
        w.main, sub=True), None, None, None),
    ('Start-Process cmd in its worktree from main stays allowed', lambda w: ps(
        "Start-Process cmd -WorkingDirectory '%s' -ArgumentList '/c echo x > f.txt'" % w.wt, w.main, sub=True),
     None, None, None),
    ('Start-Process with an unknown parameter', lambda w: ps(
        "Start-Process cmd -Bogus -ArgumentList '/c echo hi'", w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process with an unknown parameter and no program', lambda w: ps(
        "Start-Process -ArgumentList '/c echo hi' -Bogus", w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process with an ambiguous parameter prefix', lambda w: ps(
        "Start-Process cmd -e x '/c echo hi'", w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process switch given its value after a space', lambda w: ps(
        "Start-Process -Wait: $true cmd '/c echo hi'", w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process -ErrorAction Stop in its worktree stays allowed', lambda w: ps(
        "Start-Process cmd -ArgumentList '/c echo x > f.txt' -ErrorAction Stop", w.wt, sub=True), None, None, None),
    ('Start-Process -Wait in its worktree stays allowed', lambda w: ps(
        "Start-Process -Wait -NoNewWindow -PassThru cmd -ArgumentList '/c echo x > f.txt'", w.wt, sub=True),
     None, None, None),
    ('Start-Process with its own parameters in its worktree stays allowed', lambda w: ps(
        "Start-Process cmd -ArgumentList '/c echo hi' -Verb Open -WindowStyle Minimized -LoadUserProfile "
        "-UseNewEnvironment -RedirectStandardInput in.txt -RedirectStandardOutput out.txt "
        "-RedirectStandardError err.txt -Credential $c", w.wt, sub=True), None, None, None),
    ('Start-Process pwsh with a dashed list stays allowed', lambda w: ps(
        "Start-Process pwsh -ArgumentList '-NoProfile','-File','x.ps1'", w.wt, sub=True), None, None, None),
    # `--` (every later word positional), a splat (`@p`) and `--%` (the rest passed as written): the
    # guard does not read what PowerShell binds, so the call is refused
    ('Start-Process -- before cmd into main', lambda w: ps(
        "Start-Process -- cmd '/c echo x > %s'" % win(w.main, 'f.txt'), w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process of a splat into main', lambda w: ps(
        "$p = @{FilePath='cmd'; ArgumentList='/c echo x > %s'}; Start-Process @p" % win(w.main, 'f.txt'), w.wt,
        sub=True), 'deny', 'unreadable', None),
    ('Start-Process cmd with a splat into main', lambda w: ps(
        "$p = @{ArgumentList='/c echo x > %s'}; Start-Process cmd @p" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'unreadable', None),
    ('Start-Process cmd --% into main', lambda w: ps(
        "Start-Process cmd --%% /c echo -ea > %s" % win(w.main, 'f.txt'), w.wt, sub=True), 'deny', 'unreadable', None),
    ('Start-Process with en dash parameters into main', lambda w: ps(
        "Start-Process \u2013ArgumentList '/c echo x > %s' cmd \u2013Verbose" % win(w.main, 'f.txt'), w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('main-role Start-Process with an unknown parameter in main', lambda w: ps(
        "Start-Process cmd -Bogus -ArgumentList '/c echo hi'", w.main), 'deny', 'unreadable', None),
    ('Start-Process with a quoted -- in its worktree stays allowed', lambda w: ps(
        "Start-Process cmd '--','/c echo x > f.txt'", w.wt, sub=True), None, None, None),
    ('cmd start with a quoted title', lambda w: ps('cmd /c start "Godot" godot.exe --headless', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot.exe --headless')),
    ('cmd start /b /wait', lambda w: ps('cmd /c start /b /wait godot.exe --headless', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot.exe --headless')),

    # ---------------------------------------------------------------- nested shells
    ('bash -o pipefail -c godot', lambda w: bash('bash -o pipefail -c "godot --headless"', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('powershell -ex bypass -c godot', lambda w: ps('powershell -ex bypass -c "godot --headless"', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('pwsh -ep Bypass -Command checkout in main', lambda w: ps('pwsh -ep Bypass -Command "git checkout main"', w.main), 'deny', 'git-checkout', None),
    ('godot text piped into bash', lambda w: bash('echo "godot --headless" | bash', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('heredoc into bash in main', lambda w: bash("bash <<'EOF'\ngit checkout main\nEOF", w.main), 'deny', 'git-checkout', None),
    ('here-string into bash in main', lambda w: bash('bash <<< "git stash"', w.main), 'deny', 'git-stash', None),
    ('text piped into powershell -Command -', lambda w: ps('"git checkout main" | powershell -Command -', w.main), 'deny', 'git-checkout', None),
    # -File - reads the script from standard input, as -Command - does (docs/guard.md "Nested shells")
    ('text piped into pwsh -File -', lambda w: ps('"git checkout main" | pwsh -File -', w.main), 'deny', 'git-checkout', None),
    ('text piped into powershell -NoProfile -File -', lambda w: ps('"git stash" | powershell -NoProfile -File -', w.main),
     'deny', 'git-stash', None),
    ('text piped into pwsh -f -', lambda w: ps('"git checkout main" | pwsh -f -', w.main), 'deny', 'git-checkout', None),
    ('bash text piped into pwsh -File -', lambda w: bash('echo "git checkout main" | pwsh -File -', w.main), 'deny',
     'git-checkout', None),
    ('godot piped into pwsh -File -', lambda w: ps('"godot --headless" | pwsh -NoProfile -File -', w.wt), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('script piped into pwsh -File -, subagent', lambda w: ps('Get-Content x.ps1 | pwsh -File -', w.wt, sub=True),
     'deny', 'unreadable', None),
    # after a switch powershell.exe does not know, the words from it on are still judged as its command
    ('text piped into powershell -File - after an unknown switch',
     lambda w: ps('"x" | powershell -X -ExecutionPolicy "Bypass; git checkout main" -File -', w.main), 'deny',
     'git-checkout', None),
    ('text piped into powershell -Command - after an unknown switch',
     lambda w: ps('"x" | powershell -X -ExecutionPolicy "Bypass; git checkout main" -Command -', w.main), 'deny',
     'git-checkout', None),
    ('stash piped into powershell -File - after an unknown switch',
     lambda w: ps("\"x\" | powershell -X -ExecutionPolicy 'Bypass; git stash' -File -", w.main), 'deny',
     'git-stash', None),
    ('stash piped into powershell -Command - after an unknown switch',
     lambda w: ps("\"x\" | powershell -X -ExecutionPolicy 'Bypass; git stash' -Command -", w.main), 'deny',
     'git-stash', None),
    ('godot piped into powershell -File - after an unknown switch, subagent',
     lambda w: ps("\"x\" | powershell -X -ExecutionPolicy 'Bypass; godot --headless' -File -", w.wt, sub=True),
     'deny', 'heavy', None),
    ('pwsh -File script, subagent', lambda w: ps('pwsh -NoProfile -File x.ps1', w.wt, sub=True), None, None, None),
    ('script piped into bash, main role in its worktree', lambda w: bash('cat x.sh | bash', w.wt), None, None, None),
    ('script piped into bash, subagent', lambda w: bash('cat x.sh | bash', w.wt, sub=True), 'deny', 'unreadable', None),
    ('script piped into powershell with no command, subagent', lambda w: ps('Get-Content x.ps1 | powershell -NoProfile', w.wt, sub=True),
     'deny', 'unreadable', None),

    # ---------------------------------------------------------------- alternatives are commands to run
    ('godot in the background', lambda w: bash('godot --headless &', w.wt), 'deny', 'heavy', heavy('wt', 'godot --headless')),
    ('cd then godot', lambda w: bash('cd %s && godot --headless' % W.fwd(w.wt), w.main), 'deny', 'heavy',
     heavy('wt', 'godot --headless')),
    ('env prefix then godot', lambda w: bash('FOO=1 godot --headless', w.wt), 'deny', 'heavy', heavy('wt', 'godot --headless')),

    # ---------------------------------------------------------------- root searches
    ('rg --files from /', lambda w: bash('rg --files /', w.wt), 'deny', 'root-search',
     'rg --files "{wt}"'),
    ('gci -Depth before the root', lambda w: ps('Get-ChildItem -Depth 3 C:' + BS, w.wt), 'deny', 'root-search',
     'Get-ChildItem -Depth 3 "{wt}"'),
    ('gci wildcard at the root', lambda w: ps('Get-ChildItem C:' + BS + '*.cs -Recurse', w.wt), 'deny', 'root-search',
     'Get-ChildItem "{wt}/*.cs" -Recurse'),
    ('gci SystemDrive', lambda w: ps('Get-ChildItem $env:SystemDrive' + BS + ' -Recurse -Filter *.cs', w.wt), 'deny', 'root-search', None),
    ('gci HOME', lambda w: ps('gci $HOME -Recurse -Filter *.md', w.wt), 'deny', 'root-search', None),
    ('find ~', lambda w: bash('find ~ -name "*.md"', w.wt), 'deny', 'root-search', None),
    ('find under HOME is no root', lambda w: bash('find $HOME/projects -name x', w.wt), None, None, None),
    ('cmd dir /s USERPROFILE', lambda w: ps('cmd /c dir /s %USERPROFILE%', w.wt), 'deny', 'root-search', None),
    ('grep -r under ~ is no root', lambda w: bash('grep -rn foo ~/src', w.wt), None, None, None),

    # ---------------------------------------------------------------- plain commands stay allowed
    ('commit -m attached message starting -a', lambda w: bash('git commit -m"-a note"', w.main), None, None, None),
    ('commit -qm attached message', lambda w: bash('git commit -qm"fix -a case"', w.main), None, None, None),
    ('godotenv is not godot', lambda w: bash('godotenv run x', w.wt), None, None, None),
    ('brace expansion in arguments, subagent', lambda w: bash('mkdir -p src/{a,b}', w.wt, sub=True), None, None, None),
    ('ANSI-C escape in an argument, subagent', lambda w: bash("echo $'a" + BS + "tb'", w.wt, sub=True), None, None, None),
    ('godotenv in an unreadable line', lambda w: bash("echo 'x; godotenv run", w.wt), None, None, None),
    ('cd inside a subshell is followed', lambda w: bash('(cd %s && git checkout -b kit/x)' % W.fwd(w.wt), w.main), None, None, None),
    ('subshell cd does not leak', lambda w: bash('(cd %s); git checkout kit/x' % W.fwd(w.wt), w.main), 'deny', 'git-checkout', None),
    ('brace group cd carries', lambda w: bash('{ cd %s; }; git checkout -b kit/x' % W.fwd(w.wt), w.main), None, None, None),
    ('cd || then git', lambda w: bash('cd %s || true; git stash' % W.fwd(w.wt), w.main), 'deny', 'unplaced', None),
    ('variable cd in a subshell stays unknown', lambda w: bash('(cd "$WT" && git stash)', w.wt), 'deny', 'unplaced', None),
    ('Push-Location chain', lambda w: ps('Push-Location %s; git checkout -b kit/x; Pop-Location' % w.wt, w.main), None, None, None),
]
