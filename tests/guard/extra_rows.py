"""Rows for tests/guard/test_rules.py: busy-wait loops (and the unreadable alternative of a
nested bash -c wait), Windows paths in python heredocs (which pass: heredoc-winpath is cut,
docs/guard-v2.md B8), core.hooksPath changes (git config, -c,
clone -c, GIT_CONFIG_*), ending processes by name, the .NET and Gradle heavy defaults, and a
python's code, which counts as written where it runs
(docs/guard.md "Rules"). Same shape as ROWS there; the expected alternative is str.format text, so
a PowerShell brace is doubled.

Backslashes are built from BS so no editor or shell on this machine can rewrite them."""
from . import world as W
from .world import bash, event, ps

BS = chr(92)
LOCK = 'python "{kit}/tools/machine_lock.py" run'


def bg(command, cwd):
    return event('Bash', {'command': command, 'description': 'test', 'run_in_background': True}, cwd)


def heredoc(program, body):
    return "%s <<'EOF'\n%s\nEOF" % (program, body)


def heredoc_unquoted(program, body):
    return "%s <<EOF\n%s\nEOF" % (program, body)


ROWS = [
    # ---------------------------------------------------------------- busy-wait loops
    ('busy-wait: bash while sleep', lambda w: bash('while ! test -f done.txt; do sleep 5; done', w.wt),
     'deny', 'busy-wait', 'SECONDS=0; while ! test -f done.txt; do [ $SECONDS -ge 600 ] && break; sleep 5; done'),
    ('busy-wait: bash until sleep, subagent', lambda w: bash('until grep -q ok log.txt; do sleep 1; done; echo hi', w.wt, sub=True),
     'deny', 'busy-wait', 'SECONDS=0; until grep -q ok log.txt; do [ $SECONDS -ge 600 ] && break; sleep 1; done'),
    ('busy-wait: bash loop with a redirect keeps it', lambda w: bash("while true; do date; sleep 60; done > log.txt 2>&1", w.wt),
     'deny', 'busy-wait', 'SECONDS=0; while true; do [ $SECONDS -ge 600 ] && break; date; sleep 60; done > log.txt 2>&1'),
    ('busy-wait: the bound goes in the outer loop, not a nested one', lambda w: bash(
        'while true; do for i in 1 2; do sleep 1; done; done', w.wt, sub=True),
     'deny', 'busy-wait', 'SECONDS=0; while true; do [ $SECONDS -ge 600 ] && break; for i in 1 2; do sleep 1; done; done'),
    ('busy-wait: do on its own line', lambda w: bash('until test -f x\ndo\n  sleep 5\ndone', w.wt, sub=True),
     'deny', 'busy-wait', 'SECONDS=0; until test -f x\ndo [ $SECONDS -ge 600 ] && break;\n  sleep 5\ndone'),
    ('busy-wait: a loop in bash -c keeps its bash -c, single-quoted', lambda w: bash(
        'bash -c "until [ -f x ]; do sleep 5; done"', w.wt, sub=True),
     'deny', 'busy-wait', "bash -c 'SECONDS=0; until [ -f x ]; do [ $SECONDS -ge 600 ] && break; sleep 5; done'"),
    ('busy-wait: a loop in bash -c from PowerShell', lambda w: ps('bash -c "while ! test -f x; do sleep 1; done"', w.wt),
     'deny', 'busy-wait', "bash -c 'SECONDS=0; while ! test -f x; do [ $SECONDS -ge 600 ] && break; sleep 1; done'"),
    ('busy-wait: a PowerShell loop in powershell -Command', lambda w: bash(
        "powershell -Command 'while (-not (Test-Path x)) { Start-Sleep 5 }'", w.wt, sub=True),
     'deny', 'busy-wait',
     "powershell -Command '$deadline = (Get-Date).AddMinutes(10); while (-not (Test-Path x)) {{ if ((Get-Date) -gt $deadline) "
     "{{ break }}; Start-Sleep 5 }}'"),
    ('busy-wait alternative, bash, a subagent, its variable in scope', lambda w: bash(
        'F=done.txt; SECONDS=0; until [ -f "$F" ]; do [ $SECONDS -ge 600 ] && break; sleep 5; done', w.wt, sub=True),
     None, None, None),
    ('busy-wait alternative, bash, in a main checkout', lambda w: bash(
        'SECONDS=0; while ! test -f done.txt; do [ $SECONDS -ge 600 ] && break; sleep 5; done', w.main),
     None, None, None),
    ('busy-wait alternative, bash -c from PowerShell', lambda w: ps(
        "bash -c 'SECONDS=0; while ! test -f x; do [ $SECONDS -ge 600 ] && break; sleep 1; done'", w.wt, sub=True),
     None, None, None),
    # a nested bash -c whose text holds a variable is unreadable: its alternative is a script
    # file, never the refused line again
    ('unreadable: a double-quoted bash -c wait, subagent', lambda w: bash(
        'timeout 600 bash -c "until [ -f $F ]; do sleep 5; done"', w.wt, sub=True),
     'deny', 'unreadable', 'bash "{temp}/script.sh"'),
    ('unreadable: a double-quoted bash -c wait from PowerShell', lambda w: ps(
        'bash -c "until [ -f $F ]; do sleep 5; done"', w.wt, sub=True),
     'deny', 'unreadable', 'powershell -NoProfile -ExecutionPolicy Bypass -File "{temp}/script.ps1"'),
    # an odd single and double quote: the guard never rewrites the line, so the
    # alternative is the script file
    ('unreadable: an unclosed quote gives the script, subagent', lambda w: bash(
        'echo "a\'b', w.wt, sub=True),
     'deny', 'unreadable', 'bash "{temp}/script.sh"'),
    ('unreadable alternative, bash script, is allowed', lambda w: bash('bash "%s/script.sh"' % W.fwd(w.temp), w.wt, sub=True),
     None, None, None),
    ('unreadable alternative, PowerShell script, is allowed', lambda w: ps(
        'powershell -NoProfile -ExecutionPolicy Bypass -File "%s/script.ps1"' % W.fwd(w.temp), w.wt, sub=True),
     None, None, None),
    ('busy-wait: PowerShell while Start-Sleep', lambda w: ps('while (-not (Test-Path x)) { Start-Sleep -Seconds 5 }', w.wt),
     'deny', 'busy-wait',
     '$deadline = (Get-Date).AddMinutes(10); while (-not (Test-Path x)) {{ if ((Get-Date) -gt $deadline) {{ break }}; '
     'Start-Sleep -Seconds 5 }}'),
    ('busy-wait: PowerShell do until', lambda w: ps('do { Start-Sleep 2 } until (Test-Path x)', w.wt, sub=True),
     'deny', 'busy-wait',
     '$deadline = (Get-Date).AddMinutes(10); do {{ if ((Get-Date) -gt $deadline) {{ break }}; Start-Sleep 2 }} '
     'until (Test-Path x)'),
    ('busy-wait: PowerShell Thread Sleep', lambda w: ps('while ($true) { [Threading.Thread]::Sleep(500) }', w.wt),
     'deny', 'busy-wait', None),
    ('busy-wait alternative, bash, is allowed', lambda w: bash(
        'SECONDS=0; while ! test -f done.txt; do [ $SECONDS -ge 600 ] && break; sleep 5; done', w.wt),
     None, None, None),
    ('busy-wait: a timeout bound is allowed', lambda w: bash("timeout 600 bash -c 'while ! test -f done.txt; do sleep 5; done'", w.wt),
     None, None, None),
    ('busy-wait alternative, PowerShell, is allowed', lambda w: ps(
        '$deadline = (Get-Date).AddMinutes(10); while (-not (Test-Path x)) { if ((Get-Date) -gt $deadline) { break }; Start-Sleep 5 }', w.wt),
     None, None, None),
    ('busy-wait: run_in_background is the way to wait', lambda w: bg('while ! test -f done.txt; do sleep 5; done', w.wt),
     None, None, None),
    ('busy-wait: a loop sent to the background with &', lambda w: bash('while true; do date; sleep 60; done > log.txt 2>&1 &', w.wt),
     None, None, None),
    ('busy-wait: a counted loop', lambda w: bash('while [ $n -lt 10 ]; do sleep 1; n=$((n+1)); done', w.wt), None, None, None),
    ('busy-wait: a SECONDS bound', lambda w: bash('while ! test -f x && [ $SECONDS -lt 60 ]; do sleep 1; done', w.wt),
     None, None, None),
    ('busy-wait: a stopwatch bound', lambda w: ps(
        '$sw = [Diagnostics.Stopwatch]::StartNew(); while ($sw.Elapsed.TotalSeconds -lt 60) { Start-Sleep 1 }', w.wt),
     None, None, None),
    ('busy-wait: a for loop ends', lambda w: bash('for i in 1 2 3; do sleep 1; done', w.wt), None, None, None),
    ('busy-wait: foreach ends', lambda w: ps('foreach ($f in $files) { Start-Sleep 1 }', w.wt), None, None, None),
    ('busy-wait: while read reads lines', lambda w: bash('while read l; do sleep 1; done < f', w.wt), None, None, None),
    ('busy-wait: a loop with no sleep', lambda w: bash('while ! test -f x; do echo; done', w.wt), None, None, None),
    ('busy-wait: a loop in a message', lambda w: bash('git commit -m "while true; do sleep 1; done"', w.wt), None, None, None),
    ('busy-wait: while IFS= read reads lines', lambda w: bash('while IFS= read -r l; do sleep 1; done < f', w.wt),
     None, None, None),
    ('busy-wait: an assignment before a waiting test', lambda w: bash('while X=1 test ! -f x; do sleep 1; done', w.wt),
     'deny', 'busy-wait', None),

    # ---------------------------------------------------------------- Windows paths in python heredocs
    # heredoc-winpath is cut (docs/guard-v2.md B8): a quoting trap, not a hazard. Each row passes.
    ('heredoc-winpath: python - with C:' + BS + 'Users', lambda w: bash(heredoc('python -', "open('C:%sUsers%sx.txt')" % (BS, BS)), w.wt),
     None, None, None),
    ('heredoc-winpath: python3 with no dash, subagent', lambda w: bash(heredoc('python3', "p = 'D:%snew%sx'" % (BS, BS)), w.wt, sub=True),
     None, None, None),
    ('heredoc-winpath: an f-string is not raw', lambda w: bash(heredoc('python', 'p = f"C:%stmp%s{x}"' % (BS, BS)), w.wt),
     None, None, None),
    ('heredoc-winpath: a raw string', lambda w: bash(heredoc('python -', "open(r'C:%sUsers%sx.txt')" % (BS, BS)), w.wt),
     None, None, None),
    ('heredoc-winpath: forward slashes', lambda w: bash(heredoc('python -', "open('C:/tmp/x.txt')"), w.wt), None, None, None),
    ('heredoc-winpath: doubled backslashes', lambda w: bash(heredoc('python -', "open('C:%s%sUsers')" % (BS, BS)), w.wt),
     None, None, None),
    ('heredoc-winpath: a comment', lambda w: bash(heredoc('python -', '# C:%sUsers%sx%sprint(1)' % (BS, BS, '\n')), w.wt),
     None, None, None),
    ('heredoc-winpath: cat writes data', lambda w: bash(heredoc('cat > a.txt', 'C:%sUsers%sx' % (BS, BS)), w.wt), None, None, None),
    ('heredoc-winpath: python runs a file', lambda w: bash(heredoc('python x.py', "C:%sUsers%sx" % (BS, BS)), w.wt), None, None, None),
    ('heredoc-winpath: a % format before an escape', lambda w: bash(heredoc('python -', 'print("%%s:%sn" %% x)' % BS), w.wt),
     None, None, None),
    ('heredoc-winpath: a label ending in a tab', lambda w: bash(heredoc('python -', 'print("x:%st")' % BS), w.wt), None, None, None),
    ('heredoc-winpath: a label ending in a newline', lambda w: bash(heredoc('python -', 'print("a:%sn")' % BS), w.wt),
     None, None, None),
    ('heredoc-winpath: a drive and a folder', lambda w: bash(heredoc('python -', 'print("d:%sdata")' % BS), w.wt),
     None, None, None),
    ('heredoc-winpath: unquoted <<EOF turns doubled backslashes into one',
     lambda w: bash(heredoc_unquoted('python -', "open('C:%s%sUsers')" % (BS, BS)), w.wt), None, None, None),
    ('heredoc-winpath: unquoted <<EOF with four backslashes',
     lambda w: bash(heredoc_unquoted('python -', "open('C:%sUsers')" % (BS * 4)), w.wt), None, None, None),

    # ---------------------------------------------------------------- core.hooksPath (every session)
    ('git-hooks-path: config set', lambda w: bash('git config core.hooksPath /dev/null', w.wt), 'deny', 'git-hooks-path',
     'git -C "{wt}" config --get core.hooksPath'),
    ('git-hooks-path: unset, subagent', lambda w: bash('git config --unset core.hooksPath', w.wt, sub=True), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: --local, PowerShell', lambda w: ps('git config --local core.hooksPath .githooks', w.wt), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: --global from a temp folder', lambda w: bash('git config --global core.hooksPath x', w.temp),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -c override', lambda w: bash('git -c core.hooksPath=/dev/null commit -m x', w.wt), 'deny', 'git-hooks-path',
     'git commit -m x'),
    ('git-hooks-path: -c key bash unescapes', lambda w: bash('git -c core.hooksPat\\h=/dev/null commit -m x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -\\c bash unescapes', lambda w: bash('git -\\c core.hooksPath=/dev/null commit -m x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: --get', lambda w: bash('git config --get core.hooksPath', w.wt), None, None, None),
    ('git-hooks-path: a bare read', lambda w: bash('git config core.hooksPath', w.wt), None, None, None),
    ('git-hooks-path: another key', lambda w: bash('git config user.name x', w.wt), None, None, None),
    ('git-hooks-path: a scratch repo under temp', lambda w: bash('git -C %s/r config core.hooksPath x' % W.fwd(w.temp), w.wt),
     None, None, None),
    ('git-hooks-path: -f on the repo\'s own .git/config', lambda w: bash('git config -f .git/config core.hooksPath /dev/null', w.wt),
     'deny', 'git-hooks-path', 'git -C "{wt}" config --get core.hooksPath'),
    ('git-hooks-path: --file= on a worktree config', lambda w: ps(
        'git config --file=.git/worktrees/x/config.worktree core.hooksPath x', w.wt), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f on another file', lambda w: bash('git config -f tool.cfg core.hooksPath x', w.wt), None, None, None),
    ('git-hooks-path: -f on a scratch repo under temp', lambda w: bash(
        'git config -f %s/r/.git/config core.hooksPath x' % W.fwd(w.temp), w.wt), None, None, None),
    # GIT_CONFIG_PARAMETERS leaves git unplaceable, so an agent's commit meets git-main-history first,
    # which says to drop the variable and commit in the worktree the agent is in
    ('git-hooks-path: GIT_CONFIG_PARAMETERS', lambda w: bash(
        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x", w.wt, sub=True), 'deny',
     'git-main-history', 'python "{kit}/tools/commit.py" -C "{wt}" -m "<subject>" <each path you changed>'),
    ('git-hooks-path: GIT_CONFIG_PARAMETERS on a read', lambda w: bash(
        "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git status", w.wt, sub=True), 'deny', 'git-hooks-path',
     'git -C "{wt}" config --get core.hooksPath'),
    ('git-hooks-path: GIT_CONFIG_KEY_0, PowerShell', lambda w: ps("$env:GIT_CONFIG_KEY_0 = 'core.hooksPath'", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: GIT_CONFIG_PARAMETERS by .NET', lambda w: ps(
        "[Environment]::SetEnvironmentVariable('GIT_CONFIG_PARAMETERS', \"'core.hooksPath'='x'\")", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: another variable by .NET', lambda w: ps(
        "[Environment]::SetEnvironmentVariable('MY_NOTE', 'core.hooksPath')", w.wt), None, None, None),
    ('git-hooks-path: GIT_CONFIG_PARAMETERS with another key', lambda w: bash(
        "GIT_CONFIG_PARAMETERS=\"'user.name'='x'\" git commit -m x", w.wt), None, None, None),
    ('git-hooks-path: a commit message naming it', lambda w: bash(
        'git commit -m "GIT_CONFIG_PARAMETERS core.hooksPath"', w.wt), None, None, None),
    ('git-hooks-path: clone -c', lambda w: bash('git clone -c core.hooksPath=/dev/null https://example.com/r.git', w.wt),
     'deny', 'git-hooks-path', 'git clone https://example.com/r.git'),
    ('git-hooks-path: clone --config=', lambda w: bash('git clone --config=core.hooksPath=x https://example.com/r.git', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: clone -c another key', lambda w: bash('git clone -c core.autocrlf=false https://example.com/r.git', w.wt),
     None, None, None),
    ('git-hooks-path: $env: with no spaces', lambda w: ps(
        "$env:GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\"; git commit -m x", w.wt), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: ${env:} +=', lambda w: ps("${env:GIT_CONFIG_PARAMETERS} += \" 'core.hooksPath'='x'\"", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: .NET behind an assignment', lambda w: ps(
        "$x = [Environment]::SetEnvironmentVariable('GIT_CONFIG_PARAMETERS', \"'core.hooksPath'='x'\")", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: Set-Item -Value:', lambda w: ps("Set-Item env:GIT_CONFIG_KEY_0 -Value:core.hooksPath", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: bash +=', lambda w: bash("GIT_CONFIG_PARAMETERS+=\" 'core.hooksPath'='x'\" git commit -m x", w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f .git/./config', lambda w: bash('git config -f .git/./config core.hooksPath x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f .git/info/../config', lambda w: bash('git config -f .git/info/../config core.hooksPath x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f .git/worktrees/x/../config', lambda w: bash(
        'git config -f .git/worktrees/x/../config core.hooksPath x', w.wt), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f a submodule config', lambda w: bash('git config -f .git/modules/sub/config core.hooksPath x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f the user config', lambda w: bash('git config -f ~/.gitconfig core.hooksPath x', w.wt),
     'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f a path the text does not name', lambda w: bash(
        'git config -f "$(git rev-parse --git-common-dir)/config" core.hooksPath x', w.wt), 'deny', 'git-hooks-path', None),
    ('git-hooks-path: -f a dynamic path, another key', lambda w: bash(
        'git config -f "$(git rev-parse --git-common-dir)/config" user.name x', w.wt), None, None, None),
    ('git-hooks-path: -f a file named config elsewhere', lambda w: bash('git config -f tools/config core.hooksPath x', w.wt),
     None, None, None),
    ('git-hooks-path: reading $env:', lambda w: ps('Write-Output $env:GIT_CONFIG_PARAMETERS; echo core.hooksPath', w.wt),
     None, None, None),

    # ---------------------------------------------------------------- ending processes by name (agent sessions)
    ('kill-by-name: taskkill /IM, subagent', lambda w: ps('taskkill /IM godot.exe /F', w.wt, sub=True), 'deny', 'kill-by-name',
     'tasklist /FI "IMAGENAME eq godot.exe"'),
    ('kill-by-name: Stop-Process -Name, subagent', lambda w: ps('Stop-Process -Name godot -Force', w.wt, sub=True), 'deny', 'kill-by-name',
     'Get-Process -Name godot | Select-Object Id, StartTime, Path'),
    ('kill-by-name: Get-Process piped to Stop-Process', lambda w: ps('Get-Process godot | Stop-Process', w.wt, sub=True),
     'deny', 'kill-by-name', None),
    ('kill-by-name: taskkill /FI IMAGENAME', lambda w: ps('taskkill /F /FI "IMAGENAME eq godot.exe"', w.wt, sub=True),
     'deny', 'kill-by-name', None),
    ('kill-by-name: pkill', lambda w: bash('pkill -f godot', w.wt, sub=True), 'deny', 'kill-by-name', None),
    ('kill-by-name: Stop-Process -Id', lambda w: ps('Stop-Process -Id 1234 -Force', w.wt, sub=True), None, None, None),
    ('kill-by-name: taskkill /PID', lambda w: bash('taskkill //PID 1234 //F', w.wt, sub=True), None, None, None),
    ('kill-by-name: Get-Process -Id piped', lambda w: ps('Get-Process -Id 1234 | Stop-Process', w.wt, sub=True), None, None, None),
    ('kill-by-name: listing by name', lambda w: ps('Get-Process -Name godot', w.wt, sub=True), None, None, None),
    ('kill-by-name: the owner session', lambda w: ps('Stop-Process -Name godot -Force', w.wt), None, None, None),
    ('kill-by-name: pkill -P is by parent', lambda w: bash('pkill -P 1234', w.wt, sub=True), None, None, None),
    ('kill-by-name: pkill value options', lambda w: bash('pkill -g 77 -s 5 -t pts/1 -u bob', w.wt, sub=True), None, None, None),
    ('kill-by-name: pkill --parent', lambda w: bash('pkill --parent 1234', w.wt, sub=True), None, None, None),
    ('kill-by-name: killall -u', lambda w: bash('killall -u bob', w.wt, sub=True), None, None, None),
    ('kill-by-name: pkill -P with a name', lambda w: bash('pkill -P 1234 godot', w.wt, sub=True), 'deny', 'kill-by-name',
     None),
    ('kill-by-name: pkill with a signal', lambda w: bash('pkill -HUP godot', w.wt, sub=True), 'deny', 'kill-by-name', None),
    ('kill-by-name: killall -s with a name', lambda w: bash('killall -s KILL godot', w.wt, sub=True), 'deny', 'kill-by-name',
     None),
    ('kill-by-name: pkill -fu with a name', lambda w: bash('pkill -fu bob godot', w.wt, sub=True), 'deny', 'kill-by-name', None),
    ('kill-by-name: an attached value keeps the name', lambda w: bash('pkill -ubuilds godot', w.wt, sub=True),
     'deny', 'kill-by-name', None),
    ('kill-by-name: an attached value after a flag', lambda w: bash('pkill -fP1234 godot', w.wt, sub=True),
     'deny', 'kill-by-name', None),
    ('kill-by-name: an attached value alone', lambda w: bash('pkill -ubuilds', w.wt, sub=True), None, None, None),

    # ---------------------------------------------------------------- .NET and Gradle heavy defaults
    ('heavy: dotnet build', lambda w: bash('dotnet build x.sln', w.wt), 'deny', 'heavy',
     LOCK + ' --kind build --repo demo --slug wt -- dotnet build x.sln'),
    ('heavy: gradlew test, PowerShell', lambda w: ps('./gradlew test', w.wt, sub=True), 'deny', 'heavy',
     LOCK + ' --kind build --repo demo --slug wt -- ./gradlew test'),
    ('heavy: dotnet --info', lambda w: bash('dotnet --info', w.wt), None, None, None),
    ('heavy: gradlew --version', lambda w: bash('./gradlew --version', w.wt), None, None, None),

    # ---------------------------------------------------------------- python's code counts as written where it runs
    # python -c, or a program read from standard input, writes the directory python runs in, whatever
    # the code is: the guard does not read it (docs/guard.md "Shell writes")
    ('main-checkout: python -I -c that only reads json, subagent', lambda w: bash(
        'python -I -c "import json,sys; d=json.load(open(sys.argv[1])); print(len(d))" x.json', w.main, sub=True),
     'deny', 'unplaced', None),
    ('main-checkout: python -I -c read-only, PowerShell, subagent', lambda w: ps(
        'python -I -c "import json; print(json.load(open(\'x.json\', encoding=\'utf-8\'))[\'a\'])"', w.main, sub=True),
     'deny', 'unplaced', None),
    ('main-checkout: py -3 -Ic read-only', lambda w: bash(
        'py -3 -Ic "from os.path import exists; print(exists(\'x\'))"', w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -c without -I', lambda w: bash(
        'python -c "import json,sys; print(json.load(open(sys.argv[1])))" x.json', w.main, sub=True),
     'deny', 'unplaced', None),
    ('main-checkout: python -I -c with text the shell fills in', lambda w: bash(
        'python -I -c "$CODE"', w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I -cCODE in one word', lambda w: bash(
        'python -I -c"open(\'x\',\'w\').write(\'y\')"', w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I - reads its code from a pipe', lambda w: bash(
        'echo "print(1)" | python -I -', w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I -c with no quote or backslash, PowerShell', lambda w: ps(
        "python -I -c 'print(1)'", w.main, sub=True), 'deny', 'unplaced', None),
    # a script decides what it writes, but PowerShell drops the empty word after the bash -c text,
    # so the words bash, and then python, gets are not the ones read: python_unread walks out
    # through every shell around python (nested_in) to that PowerShell word
    ('main-checkout: python script in a bash -c text with an empty word, PowerShell', lambda w: ps(
        "bash -c 'python x.py' ''", w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I -c in a bash -c text, PowerShell', lambda w: ps(
        "bash -c 'python -I -c print(1)'", w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I -c through cmd and then bash', lambda w: bash(
        "cmd /c 'bash -c \"python -I -c print(1)\"'", w.main, sub=True), 'deny', 'unplaced', None),
    ('main-checkout: python -I -c through cmd, in a worktree', lambda w: bash(
        "cmd /c \"python -I -c print(open('x','w').write('y'))\"", w.wt, sub=True), None, None, None),
    # unquoted, PowerShell passes `print` and then runs the ( ) itself (shell._Scan.ps_paren_arg):
    # the text cmd gets is computed
    ('unreadable: a ( ) touching a word in a PowerShell cmd /c', lambda w: ps(
        "cmd /c python -I -c print(open('x','w').write('y'))", w.main, sub=True),
     'deny', 'unreadable', None),
    # Start-Process re-splits -ArgumentList with its quotes taken out: python reads other words
    ('main-checkout: python -I -c through Start-Process', lambda w: ps(
        "Start-Process python -ArgumentList '-I','-c','print(1)'", w.main, sub=True),
     'deny', 'unplaced', None),
    ('main-checkout: python -I -c through the saps alias', lambda w: ps(
        "saps python -ArgumentList '-I','-c','print(1)'", w.main, sub=True),
     'deny', 'unplaced', None),
    # a script decides its own writes, but PowerShell 5.1 drops an empty word, so python may take
    # the next one as its code (rules.ps_native_unsure)
    ('main-checkout: python script, PowerShell', lambda w: ps(
        'python x.py --note a', w.main, sub=True), None, None, None),
    ('main-checkout: python script beside an empty word, PowerShell', lambda w: ps(
        "python '' -c \"open('f','w').write('y')\"", w.main, sub=True), 'deny', 'unplaced', None),
]
