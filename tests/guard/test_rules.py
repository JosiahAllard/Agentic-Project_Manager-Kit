"""kitlib.guard.decide over PreToolUse-shaped payloads (docs/guard.md "Rules"): main session and
subagent, Bash and PowerShell, allow and deny, and the exact alternative each deny names.

Each row: (name, payload builder, expected kind or None for allow, rule, the text after
"Instead: "). Paths in the expected text are filled from the fake world ({main}, {wt}, ... in
forward-slash form; {main_bs} keeps backslashes)."""
import json
import os
import re
import unittest

from kitlib import lockmcp as lockmcp_names, pmdata
from kitlib.config import user as kuser
from kitlib.git import repo
from kitlib.guard import commands as cm, context, rules, shell as sh
from kitlib.lockmcp import server as lockmcp

from . import test_rules_hub as TH
from . import world as W
from .claudehost import MCP_PREFIX, decide
from .extra_rows import ROWS as EXTRA_ROWS
from .review_rows import ROWS as REVIEW_ROWS
from .shell_rows import COMMIT, LOCK, ROWS as SHELL_ROWS
from .world import bash, event, ps

ROWS = [
    # ---------------------------------------------------------------- wrapped gate (P16)
    ('gate piped to tail', lambda w: bash('python tools/gate.py --full 2>&1 | tail -5', w.wt),
     'deny', 'wrapped-gate',
     'python "{kit}/tools/gate.py" -C "{wt}" --full'),
    ('cd then gate piped (P16 shape), subagent', lambda w: bash('cd %s && python tools/gate.py 2>&1 | tail -n 40' % W.fwd(w.wt), w.main, sub=True),
     'deny', 'wrapped-gate',
     'python "{kit}/tools/gate.py" -C "{wt}"'),
    ('gate with a relative -C piped', lambda w: bash('python tools/gate.py -C . --full | tail -5', w.wt),
     'deny', 'wrapped-gate',
     'python "{kit}/tools/gate.py" -C . --full'),
    ('lock run piped, PowerShell', lambda w: ps('python ~/.claude/tools/machine_lock/machine_lock.py run --lock --kind engine-test --repo demo --slug wt -- python -m unittest 2>&1 | Select-Object -Last 20', w.wt),
     'deny', 'wrapped-gate',
     'python "{kit}/tools/machine_lock.py" run --lock --kind engine-test --repo demo --slug wt -- python -m unittest'),
    ('gate to a log', lambda w: bash('python tools/gate.py --full > "%s/g.log" 2>&1' % W.fwd(w.temp), w.wt), None, None, None),
    ('gate with || is not a pipe', lambda w: bash('python tools/gate.py || echo failed', w.wt), None, None, None),

    # ---------------------------------------------------------------- root searches
    ('find /', lambda w: bash('find / -name "*.py"', w.wt), 'deny', 'root-search',
     'find "{wt}" -name "*.py"'),
    ('find C:\\ unquoted, subagent', lambda w: bash('find C:\\ -name x', w.wt, sub=True), 'deny', 'root-search',
     'find "{wt}" -name x'),
    ('Get-ChildItem C:\\ -Recurse', lambda w: ps('Get-ChildItem C:\\ -Recurse -Filter *.py', w.wt), 'deny', 'root-search',
     'Get-ChildItem "{wt}" -Recurse -Filter *.py'),
    ('gci -Path C:/ -r', lambda w: ps('gci -Path C:/ -r', w.wt), 'deny', 'root-search',
     'gci -Path "{wt}" -r'),
    ('Get-ChildItem C:\\ not recursive', lambda w: ps('Get-ChildItem C:\\', w.wt), None, None, None),
    ('grep -r from /c/', lambda w: bash('grep -rn foo /c/', w.wt), 'deny', 'root-search',
     'grep -rn foo "{wt}"'),
    ('cmd dir /s C:\\', lambda w: ps('cmd /c dir /s /b C:\\', w.wt), 'deny', 'root-search',
     'dir /s /b "{wt}"'),
    ('find in the worktree', lambda w: bash('find . -name "*.py"', w.wt), None, None, None),
    ('root in quoted text is no search', lambda w: bash('git commit -m "do not find / -name x"', w.wt), None, None, None),

    # ---------------------------------------------------------------- heavy programs
    ('godot with no lock', lambda w: bash('godot --headless --path .', w.wt), 'deny', 'heavy',
     LOCK + ' --kind app-run --repo demo --slug wt -- godot --headless --path .'),
    ('godot.exe by path, PowerShell', lambda w: ps('& "C:\\Program Files\\Godot\\godot.exe" --headless', w.main, sub=True),
     'deny', 'heavy', LOCK + ' --kind app-run --repo demo --slug main -- "C:\\Program Files\\Godot\\godot.exe" --headless'),
    ('env flag alone is no lock', lambda w: bash('MACHINE_LOCK_RUN=r-20261006T120000Z-abcdef:1:2 godot -s x.gd', w.wt),
     'deny', 'heavy', LOCK + ' --kind app-run --repo demo --slug wt -- godot -s x.gd'),
    ('godot nested in bash -c', lambda w: bash('bash -c "godot --headless"', w.wt), 'deny', 'heavy',
     LOCK + ' --kind app-run --repo demo --slug wt -- godot --headless'),
    ('godot in Start-Process', lambda w: ps('Start-Process -FilePath godot.exe -ArgumentList "--headless"', w.wt),
     'deny', 'heavy', None),
    ('blender -b is a render', lambda w: bash('blender -b scene.blend -a', w.wt), 'deny', 'heavy',
     LOCK + ' --kind render --repo demo --slug wt -- blender -b scene.blend -a'),
    ('blender without -b is not heavy', lambda w: bash('blender scene.blend', w.wt), None, None, None),
    ('godot as an argument of machine_lock run', lambda w: bash(
        'python ~/.claude/tools/machine_lock/machine_lock.py run --lock --kind engine-run --repo demo --slug wt -- godot --headless', w.wt),
     None, None, None),
    ('godot as an argument of gate.py', lambda w: ps('python tools/gate.py --run godot --headless', w.wt), None, None, None),
    ('godot named in a message', lambda w: bash('git commit -m "godot --headless runs"', w.wt), None, None, None),

    # ---------------------------------------------------------------- git in a main checkout
    ('checkout in main', lambda w: bash('git checkout kit/wt', w.main), 'deny', 'git-checkout',
     'git -C "{main}" worktree add .claude/worktrees/<slug> <branch>'),
    ('switch via -C from a worktree, subagent', lambda w: ps('git -C %s switch main' % w.main, w.wt, sub=True), 'deny', 'git-checkout', None),
    ('checkout in a worktree', lambda w: bash('git checkout -b kit/new', w.wt), None, None, None),
    ('reset --hard in main', lambda w: ps('git reset --hard origin/main', w.main), 'deny', 'git-reset-hard',
     'git -C "{main}" reset --keep origin/main'),
    ('reset --keep in main', lambda w: bash('git reset --keep HEAD~1', w.main), None, None, None),
    ('clean -fdx in main', lambda w: bash('git clean -fdx', w.main), 'deny', 'git-clean',
     'git -C "{main}" clean -n'),
    ('clean -n in main', lambda w: bash('git clean -nd', w.main), None, None, None),
    ('stash in main', lambda w: bash('git stash', w.main), 'deny', 'git-stash',
     'git -C "{main}" worktree add .claude/worktrees/<slug> -b <branch>'),
    ('stash list in main', lambda w: bash('git stash list', w.main), None, None, None),
    ('add -A in main', lambda w: bash('git add -A', w.main), 'deny', 'git-bulk-add',
     'git -C "{main}" add -- <each path you changed>'),
    ('add . in main, PowerShell', lambda w: ps('git add .', w.main), 'deny', 'git-bulk-add', None),
    ('add by path in main', lambda w: bash('git add -- docs/a.md', w.main), None, None, None),
    ('add -A in a worktree', lambda w: bash('git add -A', w.wt), None, None, None),
    ('commit -am in main', lambda w: bash('git commit -am "x"', w.main), 'deny', 'git-commit-all',
     'git -C "{main}" add -- <each path you changed>'),
    ('commit whose message starts -a', lambda w: bash('git commit -m "-a note"', w.main), None, None, None),
    ('git in a place git cannot tell', lambda w: bash('git checkout x', w.unknown), 'deny', 'unplaced', None),
    ('git after a cd it cannot follow', lambda w: bash('cd "$WT" && git stash', w.wt), 'deny', 'unplaced', None),
    ('restore after a cd it cannot follow', lambda w: bash('cd "$WT" && git restore a.py', w.wt), 'deny', 'unplaced', None),
    ('subagent commit after a cd it cannot follow', lambda w: bash('cd "$WT" && git commit -m x', w.wt, sub=True), 'deny', 'unplaced', None),
    ('main-role commit after a cd it cannot follow', lambda w: bash('cd "$WT" && git commit -m x', w.wt), None, None, None),
    ('git in no checkout', lambda w: bash('git checkout x', w.plain), None, None, None),
    ('branch -D anywhere', lambda w: bash('git branch -D kit/old', w.wt), 'deny', 'git-branch-force-delete',
     'git branch -d kit/old'),
    ('branch --delete --force', lambda w: ps('git branch --delete --force kit/old', w.wt), 'deny', 'git-branch-force-delete', None),
    ('branch -d', lambda w: bash('git branch -d kit/old', w.wt), None, None, None),
    ('worktree remove of a dirty tree', lambda w: bash('git worktree remove .claude/worktrees/wt', w.main), 'deny',
     'git-worktree-remove', 'git -C "{wt}" status'),
    ('restore in main', lambda w: bash('git restore src/a.py', w.main), 'deny', 'git-restore',
     'git -C "{main}" restore --staged -- <path>'),
    ('restore in main, subagent', lambda w: ps('git restore --source HEAD~1 a.py', w.main, sub=True), 'deny', 'git-restore', None),
    ('restore --staged in main', lambda w: bash('git restore --staged -- a.py', w.main), None, None, None),
    ('restore -S in main', lambda w: bash('git restore -S a.py', w.main), None, None, None),
    ('restore --staged --worktree in main', lambda w: bash('git restore --staged --worktree a.py', w.main), 'deny', 'git-restore', None),
    ('restore -SW in main', lambda w: bash('git restore -SW a.py', w.main), 'deny', 'git-restore', None),
    # `--w` is `--worktree` to git; a long option the guard cannot pin may be it (fail closed)
    ('restore --staged --w in main', lambda w: bash('git restore --staged --w a.py', w.main), 'deny', 'git-restore', None),
    ('restore -S --no-staged in main', lambda w: ps('git restore -S --no-staged a.py', w.main), 'deny', 'git-restore', None),
    ('restore --staged --wor in main', lambda w: bash('git restore --staged --wor a.py', w.main), 'deny', 'git-restore', None),
    ('restore -S --no-st in main', lambda w: ps('git restore -S --no-st a.py', w.main), 'deny', 'git-restore', None),
    ('restore --staged --no- in main', lambda w: bash('git restore --staged --no- a.py', w.main), 'deny', 'git-restore', None),
    ('restore --staged --no in main', lambda w: ps('git restore --staged --no a.py', w.main), 'deny', 'git-restore', None),
    ('restore --staged --no-ov in main', lambda w: bash('git restore --staged --no-ov a.py', w.main), None, None, None),
    ('restore --staged --quiet in main', lambda w: bash('git restore --staged --quiet a.py', w.main), None, None, None),
    ('restore in a worktree', lambda w: bash('git restore a.py', w.wt), None, None, None),
    ('subagent commit in main', lambda w: bash('git commit -m "two words"', w.main, sub=True), 'deny', 'git-main-history',
     'git -C "{main}" worktree add .claude/worktrees/<slug> -b <branch>'),
    ('subagent commit in main via -C from its worktree', lambda w: bash('git -C %s commit -m "two words"' % W.fwd(w.main), w.wt, sub=True),
     'deny', 'git-main-history', 'python "{kit}/tools/commit.py" -C "{wt}" -m "<subject>" --model "<your model>" <each path you changed>'),
    ('subagent merge in main', lambda w: ps('git merge kit/wt', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent rebase in main', lambda w: bash('git rebase origin/main', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent cherry-pick in main', lambda w: bash('git cherry-pick abc123', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent revert in main', lambda w: bash('git revert abc123', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent am in main', lambda w: bash('git am x.patch', w.main, sub=True), 'deny', 'git-main-history', None),
    ('main-role commit in main', lambda w: bash('git commit -m "x"', w.main), None, None, None),
    ('subagent commit in its worktree', lambda w: bash('git commit -m "x"', w.wt, sub=True), 'deny', 'git-plain-commit',
     COMMIT),
    ('subagent log in main', lambda w: bash('git log -1', w.main, sub=True), None, None, None),

    # ---------------------------------------------------------------- push to main (push_block on)
    ('push main with no record', lambda w: bash('git push origin main', w.main), 'deny', 'git-push-main',
     'python "{kit}/tools/land.py" -C "{main}" <branch> --expect-tip <branch tip sha>'),
    ('push HEAD:main from a worktree', lambda w: bash('git push origin HEAD:main', w.wt), 'deny', 'git-push-main',
     'python "{kit}/tools/land.py" -C "{main}" kit/wt --expect-tip %s' % W.SHA_WT),
    ('push a gated commit to main', lambda w: bash('git push origin gated:main', w.wt), None, None, None),
    ('push --no-verify to main', lambda w: bash('git push --no-verify origin gated:main', w.wt), 'deny', 'git-push-main', None),
    ('force push to main', lambda w: bash('git push origin +gated:main', w.wt), 'deny', 'git-push-main', None),
    ('delete main', lambda w: bash('git push origin :main', w.wt), 'deny', 'git-push-main', 'git -C "{wt}" ls-remote origin main'),
    ('bare push from main', lambda w: ps('git push', w.main), 'deny', 'git-push-main', None),
    ('push a branch', lambda w: bash('git push -u origin kit/wt', w.wt), None, None, None),

    # ---------------------------------------------------------------- edits in a main checkout
    ('subagent Edit in main', lambda w: event('Edit', {'file_path': os.path.join(w.main, 'src', 'a.py'),
                                                      'old_string': 'a', 'new_string': 'b'}, w.wt, sub=True),
     'deny', 'main-checkout', 'Edit(file_path="{wt}/src/a.py")'),
    ('subagent Write in main from the main checkout', lambda w: event('Write', {'file_path': os.path.join(w.main, 'b.md'), 'content': 'x'}, w.main, sub=True),
     'deny', 'main-checkout', 'Write(file_path="<worktree>/b.md")'),
    ('subagent NotebookEdit in main', lambda w: event('NotebookEdit', {'notebook_path': os.path.join(w.main, 'n.ipynb'), 'new_source': ''}, w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('subagent Edit in its worktree', lambda w: event('Edit', {'file_path': os.path.join(w.wt, 'a.py'), 'old_string': 'a', 'new_string': 'b'}, w.wt, sub=True),
     None, None, None),
    ('subagent Edit where git cannot tell', lambda w: event('Edit', {'file_path': os.path.join(w.unknown, 'a.py'), 'old_string': 'a', 'new_string': 'b'}, w.wt, sub=True),
     'deny', 'unplaced', None),
    ('subagent Edit outside any repo, run from there', lambda w: event('Write', {'file_path': os.path.join(w.plain, 'a.txt'), 'content': ''}, w.plain, sub=True),
     None, None, None),
    ('subagent Edit outside any repo, run from its worktree', lambda w: event('Write', {'file_path': os.path.join(w.plain, 'a.txt'), 'content': ''}, w.wt, sub=True),
     'deny', 'write-jail', 'Write(file_path="{wt}/a.txt")'),
    ('main session Edit in main', lambda w: event('Edit', {'file_path': os.path.join(w.main, 'a.py'), 'old_string': 'a', 'new_string': 'b'}, w.main),
     None, None, None),
    ('subagent redirect into main', lambda w: bash('echo hi > %s/out.txt' % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout',
     'echo hi > "{wt}/out.txt"'),
    ('subagent relative redirect in main', lambda w: bash('python x.py > out.log 2>&1', w.main, sub=True), 'deny', 'main-checkout', None),
    ('subagent redirect to /dev/null', lambda w: bash('git status > /dev/null 2>&1', w.main, sub=True), None, None, None),
    ('subagent Set-Content into main', lambda w: ps('Set-Content -Path %s\\x.txt -Value 1' % w.main, w.wt, sub=True), 'deny', 'main-checkout',
     'Set-Content -Path "{wt}/x.txt" -Value 1'),
    ('subagent Out-File into main', lambda w: ps('"x" | Out-File -Encoding utf8 %s\\o.txt' % w.main, w.wt, sub=True), 'deny', 'main-checkout', None),
    ('subagent rm in main', lambda w: bash('rm -f %s/stale.txt' % W.fwd(w.main), w.wt, sub=True), 'deny', 'main-checkout', None),
    ('subagent Remove-Item in main', lambda w: ps('Remove-Item %s\\stale.txt -Force' % w.main, w.wt, sub=True), 'deny', 'main-checkout', None),
    # a PowerShell target that is not literal can name the main checkout: refused from a worktree too
    ('subagent write to a variable path', lambda w: ps('"x" > $env:OUT', w.wt, sub=True), 'deny', 'unplaced',
     '"x" > "{wt}/<file>"'),
    ('subagent bash write to a variable path', lambda w: bash('echo x > "$OUT"', w.wt, sub=True), None, None, None),
    ('subagent redirect in its worktree', lambda w: bash('echo hi > notes.txt', w.wt, sub=True), None, None, None),
    ('subagent redirect in a heredoc body is data', lambda w: bash("cat > notes.txt <<'EOF'\nx > %s/y.txt\nEOF" % W.fwd(w.main), w.wt, sub=True),
     None, None, None),
    ('main session redirect into main', lambda w: bash('echo hi > out.txt', w.main), None, None, None),

    # ---------------------------------------------------------------- unreadable lines (fallback)
    ('unclosed quote hiding a checkout', lambda w: bash("echo 'x ; git checkout main", w.main), 'deny', 'git-checkout',
     'git -C "{main}" worktree add .claude/worktrees/<slug> <branch>'),
    ('unclosed quote in a worktree', lambda w: bash("echo 'x ; git checkout main", w.wt), None, None, None),
    ('unclosed quote hiding a restore', lambda w: bash("echo 'x ; git restore a.py", w.main), 'deny', 'git-restore', None),
    ('unclosed quote hiding restore --staged', lambda w: bash("echo 'x ; git restore --staged a.py", w.main), None, None, None),
    ('unclosed quote hiding restore -SW', lambda w: bash("echo 'x ; git restore -SW a.py", w.main), 'deny', 'git-restore', None),
    ('unclosed quote hiding a subagent merge', lambda w: bash("echo 'x ; git merge kit/wt", w.main, sub=True), 'deny', 'git-main-history', None),
    ('unclosed quote hiding a main-role merge', lambda w: bash("echo 'x ; git merge kit/wt", w.main), 'deny', 'unreadable',
     'bash "{temp}/script.sh"'),
    ('unclosed quote hiding a subagent merge in its worktree', lambda w: bash("echo 'x ; git merge kit/wt", w.wt, sub=True), 'deny', 'unreadable', None),
    ('cmd /c with a trailing backslash root', lambda w: bash('cmd /c "dir /s C:\\"', w.wt), 'deny', 'root-search', None),
    ('variable program running godot', lambda w: bash('GODOT=/c/tools/godot.exe; $GODOT --headless', w.wt), 'deny', 'heavy',
     LOCK + ' --kind app-run --repo demo --slug wt -- <the program $GODOT names> --headless'),

    # ---------------------------------------------------------------- backlog roles (tests/guard/test_backlog_role.py)
    ('subagent builder closes a record', lambda w: bash('python "%s/tools/backlog.py" -C "%s" close X-1'
                                                        % (W.fwd(w.kit_dir), W.fwd(w.wt)), w.wt, sub=True),
     'deny', 'backlog-role', 'python "{kit}/tools/backlog.py" -C "{wt}" show X-1'),
    ('subagent builder adds a record', lambda w: bash('python "%s/tools/backlog.py" add --summary s'
                                                      % W.fwd(w.kit_dir), w.wt, sub=True), None, None, None),
    ('subagent code importing the backlog library', lambda w: bash('python -c "from kitlib.backlog import store"',
                                                                   w.wt, sub=True),
     'deny', 'backlog-raw-write', 'python "{kit}/tools/backlog.py" -C <worktree> <verb> <arguments>'),
    ('subagent push of the backlog store ref', lambda w: bash('git push origin HEAD:refs/backlog/main', w.wt, sub=True),
     'deny', 'backlog-raw-write', 'python "{kit}/tools/backlog.py" -C <worktree> <verb> <arguments>'),

    # ---------------------------------------------------------------- git run by interpreter code
    ('subagent heredoc python running git update-ref', lambda w: bash(
        "python - <<'EOF'\nimport subprocess\nsubprocess.run(['git', '-C', w, 'update-ref', ref, c])\nEOF",
        w.wt, sub=True), 'deny', 'interpreter-git', 'git -C "{wt}" <the git command your code ran>'),
    ('subagent python -c committing through a shell string', lambda w: bash(
        'python -c "import subprocess; subprocess.run(\'git commit -m x\', shell=True)"', w.wt, sub=True),
     'deny', 'interpreter-git', COMMIT),
    ('subagent python -c merging through an f-string', lambda w: bash(
        'python -c "import subprocess; subprocess.run(f\'git -C {w} merge x\', shell=True)"', w.wt, sub=True),
     'deny', 'interpreter-git', 'python "{kit}/tools/sync_base.py" -C "{wt}" <hash> --model "<your model>"'),
    ('owner python -c running git', lambda w: bash(
        'python -c "import subprocess; subprocess.run([\'git\', \'push\'])"', w.wt), None, None, None),
] + SHELL_ROWS + REVIEW_ROWS + EXTRA_ROWS

# An alternative is a command to run as written: no closing full stop, no leading prose word
# (docs/guard.md "Rules"). The prose goes in its own sentence before "Instead:".
PROSE_WORDS = ('run', 'work', 'make', 'read', 'write', 'commit', 'merge', 'name', 'nothing', 'fix', 'ask', 'the',
               'as', 'from', 'reproduce', 'approve', 'stage', 'search', 'use')


setUpModule = W.deny_softened           # these pin what the guard cannot place and the dropped rules (world.deny_softened)
tearDownModule = W.allow_unplaced


class RuleTable(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.w.git.ancestors.add((W.SHA_WT, 'main'))

    def fill(self, text):
        w = self.w
        return text.format(main=W.fwd(w.main), wt=W.fwd(w.wt), temp=W.fwd(w.temp), home=W.fwd(w.home),
                           kit=W.fwd(w.kit_dir), main_bs=w.main)

    def test_rows(self):
        names = [r[0] for r in ROWS]
        self.assertEqual(len(names), len(set(names)))
        for name, make, kind, rule, instead in ROWS:
            with self.subTest(name):
                ev = make(self.w)
                d = decide(ev, self.w.ctx())
                if kind is None:
                    self.assertIsNone(d, d and d.reason)
                    continue
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), (kind, rule), d.reason)
                role = 'subagent' if ev.get('agent_id') else 'main'
                self.assertTrue(d.reason.endswith(rules.tag(rule, role)), d.reason)
                self.assertIn(' Instead: ', d.reason)
                got = d.reason.split(' Instead: ', 1)[1][:-len(rules.tag(rule, role))].strip()
                self.assertFalse(got.endswith('.') and not got.endswith(' .'), got)
                self.assertNotIn(got.split(' ', 1)[0].lower(), PROSE_WORDS, got)
                if instead is not None:
                    self.assertEqual(got, self.fill(instead))

    def test_unread_fallback_says_why(self):
        d = decide(bash("echo 'x ; git checkout main", self.w.main), self.w.ctx())
        why = 'could not read this command; write the program name literally. Why: an unclosed'
        self.assertIn(why, d.reason)
        for cmd, sub in (("echo 'x ; git restore a.py", False), ("echo 'x ; git merge kit/wt", True)):
            d = decide(bash(cmd, self.w.main, sub), self.w.ctx())
            self.assertIn(why, d.reason, cmd)


class LockHeld(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_a_held_lock_allows_heavy(self):
        self.w.held = True
        self.assertIsNone(decide(bash('godot --headless', self.w.wt), self.w.ctx()))
        self.assertIsNone(decide(bash("echo 'x; godot --headless", self.w.wt), self.w.ctx()))

    # the variable naming a run with no record: tests/guard/test_lock_held.py (a scratch store)

    def test_unset_variable(self):
        ctx = self.w.ctx(lock_held=None, environ={})
        self.assertEqual(ctx.lock_held(), (False, 'MACHINE_LOCK_RUN is not set: this session runs under no lock'))


class Ledger(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        os.makedirs(os.path.dirname(self.w.ledger))
        self.alt = ('python "%s/tools/ledger.py" add --repo <repo> --prov <prov> ' % W.fwd(self.w.kit_dir) +
                    '--q "<question>" --a "<answer>" --decides "<what it decides>" --source "<source>" ')

    def check(self, ev, rule='ledger'):
        d = decide(ev, self.w.ctx())
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, rule, d.reason)
        if rule == 'ledger':
            self.assertIn("Add the entry with the kit's tools/ledger.py add (docs/ledger.md). Instead: " + self.alt, d.reason)

    def test_every_role_is_denied_the_ledger(self):
        self.w.user_config({'ledger_path': self.w.ledger})
        self.check(event('Edit', {'file_path': self.w.ledger, 'old_string': 'a', 'new_string': 'b'}, self.w.plain))
        self.check(event('Write', {'file_path': self.w.ledger, 'content': ''}, self.w.plain, sub=True))
        self.check(bash('echo "- a new entry" >> %s' % W.fwd(self.w.ledger), self.w.plain))
        self.check(ps('Add-Content -Path %s -Value x' % self.w.ledger, self.w.plain))

    def test_a_strict_reader_refusal_still_yields_the_ledger(self):
        # the machine config holds a key this kit's schema does not know (pm_start and
        # coordinator_start became known keys on kit main, so an unknown one is named here)
        self.w.user_config({'ledger_path': self.w.ledger, 'pm_start': {}, 'coordinator_start': {},
                            'a_key_this_kit_does_not_know': {}})
        self.check(event('Write', {'file_path': self.w.ledger, 'content': ''}, self.w.plain))
        self.assertIn('error', self.w.ctx().user())

    def write_config(self, raw):
        """`raw` as the PM repository's config.json (World.user_config's Location)."""
        self.w.user_config({})
        with open(self.w.config_file, 'wb') as f:
            f.write(raw)

    def test_a_broken_machine_config_denies_every_subagent_write(self):
        target = os.path.join(self.w.wt, 'a.py')
        for raw, why in ((b'{', 'not valid JSON'), (b'{"ledger_path": "' + bytes([0xff]) + b'"}', 'cannot read'),
                         (b'[]', 'must be an object'), (b'{"ledger_path": 5}', 'ledger_path')):
            with self.subTest(raw):
                self.write_config(raw)
                for ev in (event('Write', {'file_path': target, 'content': ''}, self.w.wt, sub=True),
                           event('Edit', {'file_path': target, 'old_string': 'a', 'new_string': 'b'}, self.w.wt, sub=True),
                           bash('echo x > notes.txt', self.w.wt, sub=True)):
                    d = decide(ev, self.w.ctx())
                    self.assertIsNotNone(d, ev['tool_name'])
                    self.assertEqual(d.rule, 'ledger', d.reason)
                    self.assertIn('The owner or the PM session fixes it. Instead: python -m json.tool "%s" '
                                  % W.fwd(self.w.config_file), d.reason)
                    self.assertIn(why, d.reason)
                # the main session is asked (the ledger may be at any path: rules.ledger_unread); reads stay open
                d = decide(event('Write', {'file_path': target, 'content': ''}, self.w.wt), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('ask', 'ledger'), d.reason)
                self.assertIsNone(decide(bash('git status', self.w.wt, sub=True), self.w.ctx()))

    def test_a_relative_ledger_path_is_read_against_the_pm_repository(self):
        self.write_config(b'{"ledger_path": "rulings/LEDGER.md"}')
        ledger = os.path.join(self.w.pm_repo, 'rulings', 'LEDGER.md')
        self.assertEqual(self.w.ctx().user()['ledger_path'], ledger)
        self.assertNotIn('broken', self.w.ctx().user())
        d = decide(event('Write', {'file_path': ledger, 'content': ''}, self.w.plain, sub=True), self.w.ctx())
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)

    def test_unknown_keys_alone_do_not_break_it(self):
        self.w.user_config({'ledger_path': self.w.ledger, 'pm_start': {}})
        self.assertIsNone(decide(event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': ''},
                                       self.w.wt, sub=True), self.w.ctx()))

    def test_a_missing_file_stays_allowed(self):
        self.assertIsNone(decide(event('Write', {'file_path': os.path.join(self.w.wt, 'a.py'), 'content': ''},
                                       self.w.wt, sub=True), self.w.ctx()))

    def test_no_ledger_path_no_rule(self):
        self.assertIsNone(decide(event('Write', {'file_path': self.w.ledger, 'content': ''}, self.w.plain), self.w.ctx()))


class PmDataPlace(unittest.TestCase):
    """The PM data worktree (docs/pm-data-branch.md "Guard place and commit hooks"): git calls it
    LINKED; the guard answers it as MAIN, with its own .kit.json."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.common = os.path.join(self.w.main, '.git')

    def configure(self):
        self.w.user_config({'pm_data': {'path': self.w.data}})

    def head(self, git_dir, branch):
        os.makedirs(git_dir, exist_ok=True)
        with open(os.path.join(git_dir, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/%s\n' % branch)

    def record(self):
        os.makedirs(self.common, exist_ok=True)
        pmdata.write(self.common, 'pmdata', 'private', 'a' * 40)

    def write(self, path, cwd, sub=True):
        return event('Write', {'file_path': path, 'content': ''}, cwd, sub=sub)

    def test_subagent_edit_under_pm_data_denied(self):
        self.configure()
        target = os.path.join(self.w.data, 'rulings', 'LEDGER.md')
        for cwd in (self.w.data, self.w.wt):
            with self.subTest(cwd):
                d = decide(self.write(target, cwd), self.w.ctx())
                self.assertIsNotNone(d)
                self.assertEqual(d.rule, 'main-checkout', d.reason)
                self.assertIn('in the main checkout %s' % self.w.data, d.reason)
        d = decide(bash('echo x > notes.md', self.w.data, sub=True), self.w.ctx())
        self.assertEqual(d.rule, 'main-checkout', d and d.reason)
        # the PM's own session edits it
        self.assertIsNone(decide(self.write(target, self.w.data, sub=False), self.w.ctx()))

    def test_git_reset_hard_in_pm_data_denied(self):
        self.configure()
        for sub in (False, True):
            d = decide(bash('git reset --hard', self.w.data, sub=sub), self.w.ctx())
            self.assertEqual(d.rule, 'git-reset-hard', d and d.reason)
            # a subagent may not change a main checkout, so reset --keep there is refused too
            # (git-main-history): its worktree's form instead
            self.assertIn('Instead: git -C "%s" reset --keep HEAD' % ('<worktree>' if sub else W.fwd(self.w.data)),
                          d.reason)
        for cmd, rule in (('git clean -fdx', 'git-clean'), ('git stash', 'git-stash'), ('git add -A', 'git-bulk-add'),
                          ('git checkout main', 'git-checkout')):
            self.assertEqual(decide(bash(cmd, self.w.data, sub=True), self.w.ctx()).rule, rule, cmd)

    def test_pm_data_config_is_its_own(self):
        self.configure()
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(os.path.join(self.w.data, 'x.md')).kind, repo.MAIN)
        self.assertEqual(ctx.main_top(os.path.join(self.w.data, 'x.md')), self.w.data)
        self.assertEqual(ctx.config(self.w.data).main_branch, 'pmdata')
        self.assertEqual(ctx.config(self.w.wt).main_branch, 'main')
        # without its own .kit.json the defaults hold: a subagent may not edit it
        os.remove(os.path.join(self.w.data, '.kit.json'))
        d = decide(self.write(os.path.join(self.w.data, 'x.md'), self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'main-checkout', d and d.reason)

    def test_other_worktree_unchanged_with_pm_data(self):
        self.configure()
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(self.w.wt).kind, repo.LINKED)
        self.assertFalse(ctx.is_pm_data(self.w.wt))
        self.assertEqual(ctx.main_top(self.w.wt), self.w.main)
        self.assertIsNone(decide(self.write(os.path.join(self.w.wt, 'a.py'), self.w.wt), self.w.ctx()))
        self.assertIsNone(decide(bash('git add a.py', self.w.wt, sub=True), self.w.ctx()))

    def test_without_pm_data_the_worktree_stays_linked(self):
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(self.w.data).kind, repo.LINKED)
        self.assertFalse(ctx.is_pm_data(self.w.data))
        self.assertIsNone(decide(self.write(os.path.join(self.w.data, 'x.md'), self.w.data), self.w.ctx()))
        # a record whose branch the worktree's HEAD is not on changes nothing
        self.record()
        self.head(self.w.data_git_dir, 'kit/other')
        self.assertEqual(self.w.ctx().place(self.w.data).kind, repo.LINKED)

    def test_record_signal_promotes_without_machine_config(self):
        self.record()
        self.head(self.w.data_git_dir, 'pmdata')
        self.head(os.path.join(self.common, 'worktrees', 'wt'), 'kit/wt')
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(self.w.data).kind, repo.MAIN)
        self.assertTrue(ctx.is_pm_data(self.w.data))
        self.assertEqual(ctx.place(self.w.wt).kind, repo.LINKED)
        d = decide(self.write(os.path.join(self.w.data, 'x.md'), self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'main-checkout', d and d.reason)

    def test_malformed_pm_data_denies_subagent_writes(self):
        target = os.path.join(self.w.wt, 'a.py')
        self.w.user_config({'pm_data': {'path': 'relative/pmdata'}})
        for ev in (self.write(target, self.w.wt), bash('echo x > notes.txt', self.w.wt, sub=True),
                   bash('git commit -m x', self.w.wt, sub=True)):
            d = decide(ev, self.w.ctx())
            self.assertIsNotNone(d, ev['tool_input'])
            self.assertEqual(d.rule, 'pm-data', d.reason)
            self.assertIn('absolute path', d.reason)
        # the main session and reads stay open
        self.assertIsNone(decide(self.write(target, self.w.wt, sub=False), self.w.ctx()))
        self.assertIsNone(decide(bash('git status', self.w.wt, sub=True), self.w.ctx()))
        # a record that does not read: writes in that repository's worktrees are denied too
        self.w.user_config({})
        os.makedirs(self.common, exist_ok=True)
        with open(pmdata.record_path(self.common), 'w', encoding='utf-8') as f:
            f.write('{')
        d = decide(self.write(target, self.w.wt), self.w.ctx())
        self.assertEqual(d.rule, 'pm-data', d and d.reason)
        self.assertIn('not valid JSON', d.reason)
        # a machine config the strict reader refuses for another key, without pm_data, does not
        self.w.user_config({'a_key_this_kit_does_not_know': {}})
        os.remove(pmdata.record_path(self.common))
        self.assertIsNone(decide(self.write(target, self.w.wt), self.w.ctx()))

    def test_no_wt_add_alternative_in_pm_data(self):
        self.configure()
        cases = ((self.write(os.path.join(self.w.data, 'x.md'), self.w.data), 'main-checkout'),
                 (self.write(os.path.join(self.w.data, 'x.md'), self.w.wt), 'main-checkout'),
                 (bash('git merge kit/wt', self.w.data, sub=True), 'git-main-history'),
                 (bash('git restore a.md', self.w.data, sub=True), 'git-restore'),
                 (bash('git checkout main', self.w.data), 'git-checkout'),
                 (bash('git stash', self.w.data, sub=True), 'git-stash'))
        for ev, rule in cases:
            with self.subTest(rule):
                d = decide(ev, self.w.ctx())
                self.assertEqual(d.rule, rule, d and d.reason)
                self.assertIn('Agents do not work in the PM data worktree "%s"' % W.fwd(self.w.data), d.reason)
                self.assertIn('To see its state: Instead: git -C "%s" status ' % W.fwd(self.w.data), d.reason)
                self.assertNotIn('worktree add', d.reason)
        # the kit's own main checkout still offers one
        d = decide(bash('git stash', self.w.main, sub=True), self.w.ctx())
        self.assertIn('Instead: git -C "%s" worktree add' % W.fwd(self.w.main), d.reason)

    def test_push_from_pm_data_is_judged_by_the_kit_for_kit_refs(self):
        self.configure()
        for cmd in ('git push --no-verify origin main', 'git push --force origin main', 'git push origin +main'):
            for sub in (False, True):
                d = decide(bash(cmd, self.w.data, sub=sub), self.w.ctx())
                self.assertIsNotNone(d, cmd)
                self.assertEqual(d.rule, 'git-push-main', d.reason)
                self.assertIn('to origin/main', d.reason)
        # the data branch: the data worktree's own .kit.json (push_block off) allows it
        self.assertIsNone(decide(bash('git push --force origin pmdata', self.w.data), self.w.ctx()))
        with open(os.path.join(self.w.data, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'repo': {'name': 'data', 'main_branch': 'pmdata', 'push_block': True}}, f)
        d = decide(bash('git push --force origin pmdata', self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'git-push-main', d and d.reason)
        self.assertIn('to origin/pmdata', d.reason)
        # a kit .kit.json that does not load: refused
        with open(os.path.join(self.w.main, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write('{')
        d = decide(bash('git push origin kit/x', self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'git-push-main', d and d.reason)
        self.assertIn('cannot be read', d.reason)

    def test_every_ref_push_from_pm_data_is_judged_by_the_kit(self):
        self.configure()
        cases = (('git push --mirror origin', 'rewrites main'),         # --mirror forces every ref
                 ('git push --all origin', 'no gate-OK record'),
                 ('git push --delete origin main', 'main is never deleted'),
                 ('git push origin :main', 'main is never deleted'),
                 ('git push --no-verify origin v1:refs/heads/main', 'skips the pre-push hook'))
        for cmd, why in cases:
            with self.subTest(cmd):
                d = decide(bash(cmd, self.w.data), self.w.ctx())
                self.assertEqual(d.rule, 'git-push-main', d and d.reason)
                self.assertIn('to origin/main', d.reason)
                self.assertIn(why, d.reason)
        # a tag push touches no branch: allowed, as from the kit's main checkout
        for where in (self.w.data, self.w.main):
            self.assertIsNone(decide(bash('git push origin refs/tags/v1', where), self.w.ctx()), where)

    def test_unreadable_push_line_from_pm_data_is_read_again(self):
        self.configure()
        line = "echo 'x ; git push --no-verify origin main"
        d = decide(bash(line, self.w.data), self.w.ctx())
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.rule, 'git-push-main', d.reason)
        self.assertIn('could not read this command', d.reason)
        self.assertIn('skips the pre-push hook', d.reason)
        # a kit .kit.json that does not load: the line is still read again, and refused
        with open(os.path.join(self.w.main, '.kit.json'), 'w', encoding='utf-8') as f:
            f.write('{')
        d = decide(bash(line, self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'git-push-main', d and d.reason)
        self.assertIn('cannot be read', d.reason)

    def test_worktree_remove_from_pm_data_is_judged_by_the_kit(self):
        self.configure()
        self.w.git.clean[os.path.normcase(os.path.abspath(self.w.wt))] = True
        cmd = 'git worktree remove %s' % W.fwd(self.w.wt)
        d = decide(bash(cmd, self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'git-worktree-remove', d and d.reason)
        self.assertIn('not merged into main', d.reason)
        self.w.git.ancestors.add((W.SHA_WT, 'main'))
        self.assertIsNone(decide(bash(cmd, self.w.data), self.w.ctx()))

    def test_a_bad_unrelated_key_does_not_hide_pm_data(self):
        self.w.user_config({'pm_data': {'path': self.w.data}, 'a_key_this_kit_does_not_know': 1})
        ctx = self.w.ctx()
        self.assertTrue(ctx.is_pm_data(self.w.data))
        self.assertIsNone(ctx.pm_data_error(self.w.wt))
        self.assertIsNone(decide(self.write(os.path.join(self.w.wt, 'a.py'), self.w.wt), self.w.ctx()))
        self.w.user_config({'pm_data': {'path': self.w.data, 'branch': 'a b'}, 'a_key_this_kit_does_not_know': 1})
        d = decide(self.write(os.path.join(self.w.wt, 'a.py'), self.w.wt), self.w.ctx())
        self.assertEqual(d.rule, 'pm-data', d and d.reason)
        self.assertIn("branch must be a branch or remote name, not 'a b'", d.reason)

    def test_machine_config_decides_alone_when_set(self):
        self.w.user_config({'pm_data': {'path': self.w.plain}})
        self.record()
        self.head(self.w.data_git_dir, 'pmdata')
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(self.w.data).kind, repo.LINKED)
        # nor is a bad record read then
        with open(pmdata.record_path(self.common), 'w', encoding='utf-8') as f:
            f.write('{')
        self.assertIsNone(decide(self.write(os.path.join(self.w.wt, 'a.py'), self.w.wt), self.w.ctx()))

    def test_machine_path_matches_case_and_trailing_separator(self):
        self.w.user_config({'pm_data': {'path': self.w.data.upper() + os.sep}})
        ctx = self.w.ctx()
        self.assertEqual(ctx.place(self.w.data).kind, repo.MAIN)
        self.assertEqual(ctx.pm_data_branch(self.w.data), 'pmdata')

    def test_unreadable_head_denies_subagent_writes(self):
        self.record()                           # no HEAD file in the data worktree's git dir
        d = decide(self.write(os.path.join(self.w.data, 'x.md'), self.w.data), self.w.ctx())
        self.assertEqual(d.rule, 'pm-data', d and d.reason)
        self.assertIn('cannot read', d.reason)
        self.assertIn('HEAD', d.reason)

    def test_bad_record_seen_only_through_the_target(self):
        os.makedirs(self.common, exist_ok=True)
        with open(pmdata.record_path(self.common), 'w', encoding='utf-8') as f:
            f.write('{')
        d = decide(self.write(os.path.join(self.w.wt, 'a.py'), self.w.plain), self.w.ctx())
        self.assertEqual(d.rule, 'pm-data', d and d.reason)
        # a write outside that repository, from outside it, is not this rule's
        d = decide(self.write(os.path.join(self.w.plain, 'a.py'), self.w.plain), self.w.ctx())
        self.assertTrue(d is None or d.rule != 'pm-data', d and d.reason)


class LockMcp(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def ev(self, name, sub=False, prefix=MCP_PREFIX):
        return event(prefix + name, {'id': 'h-1'}, self.w.wt, sub=sub)

    def test_owner_only_tools(self):
        self.assertIn('lock_hold_create', lockmcp.OWNER_ONLY)
        for name in lockmcp.OWNER_ONLY:
            with self.subTest(name):
                d = decide(self.ev(name, sub=True), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'lock-mcp'))
                self.assertIn('This session is subagent.', d.reason)
                d = decide(self.ev(name), self.w.ctx(environ={context.ROLE_ENV: 'coordinator'}))
                self.assertEqual(d.verdict, 'deny')
                self.assertIn('This session is coordinator.', d.reason)
                self.assertIn('Instead: python "%s/tools/machine_lock.py" status ' % W.fwd(self.w.kit_dir), d.reason)
                # a session nothing marks is the owner's own (docs/guard.md "Session mark")
                self.assertIsNone(decide(self.ev(name), self.w.ctx(environ={})))

    def test_read_tools_stay_open(self):
        self.assertIsNone(decide(self.ev('lock_snapshot', sub=True), self.w.ctx()))

    def test_another_server_name_is_still_matched(self):
        d = decide(event('mcp__machine_lock__lock_end_run', {'id': 'r-1'}, self.w.wt, sub=True), self.w.ctx())
        self.assertEqual(d.rule, 'lock-mcp')

    def test_the_prefix_comes_from_the_machine_config(self):
        self.w.user_config({'guard_mcp_prefix': 'mcp__lockx__'})
        self.assertEqual(rules.mcp_prefix(self.w.ctx()), 'mcp__lockx__')
        self.assertEqual(rules.mcp_prefix(W.World().ctx()), MCP_PREFIX)
        self.assertEqual(MCP_PREFIX, lockmcp_names.tool_prefix())

    def test_the_role_resolver_is_pluggable(self):
        ctx = self.w.ctx(role_resolver=lambda ev, env: 'owner-verified-by-test')
        d = decide(self.ev('lock_set_games'), ctx)
        self.assertEqual(d.verdict, 'deny')
        self.assertIn('owner-verified-by-test', d.reason)


LIVE = 'lock/machine_lock.py'
LIVE_WIN = 'C:\\tools\\lock\\machine_lock.py'
UI = 'C:/tools/lock/reserve_ui.pyw'
UI_WIN = 'C:\\tools\\lock\\reserve_ui.pyw'
UI_REL = 'tools/machine_lock/reserve_ui.pyw'
OWNER_ONLY = 'owner-only lock tools are run by the owner, not agents'
MARKED = {context.ROLE_ENV: 'coordinator'}         # a main session the launcher marked: not the owner's
UNHELD = {context.ROLE_ENV: 'plain'}             # a marked session the write jail does not hold


class OwnerLockTools(unittest.TestCase):
    """reserve_ui.pyw and the lock CLI's owner-only writes are the owner's to run themselves; an agent's
    Bash or PowerShell call that runs one is denied, whatever the session."""

    BLOCKED = [
        # reserve_ui, any launcher, any path, with or without .pyw
        ('bash', 'pythonw %s' % UI),
        ('bash', 'python ~/lock/reserve_ui.pyw'),
        ('ps', 'py -3 %s' % UI_WIN),
        ('ps', 'start pythonw reserve_ui.pyw'),
        ('ps', 'Start-Process pythonw -ArgumentList "%s"' % UI),
        ('ps', 'Start-Process -FilePath "%s"' % UI_WIN),
        ('bash', '~/lock/reserve_ui.pyw'),
        ('ps', '& "C:/tools/lock/reserve_ui"'),
        ('bash', 'cmd //c start "" pythonw Reserve_UI.PYW'),
        ('bash', 'bash -c "nohup pythonw ./reserve_ui.pyw &"'),
        ('bash', 'nohup pythonw %s &' % UI),
        ('bash', 'start pythonw reserve_ui.pyw'),
        ('ps', 'cmd /c start "" reserve_ui.pyw'),
        ('ps', 'Invoke-Item %s' % UI_WIN),
        ('ps', 'ii reserve_ui.pyw'),
        ('bash', 'pyw reserve_ui.pyw'),
        ('ps', 'pyw -3 reserve_ui.pyw'),
        ('ps', 'pyw.exe %s' % UI_WIN),
        ('bash', 'py -u reserve_ui.pyw'),
        ('bash', 'start /b pythonw reserve_ui.pyw'),
        ('ps', 'cmd /c start /b pythonw reserve_ui.pyw'),
        ('ps', 'explorer reserve_ui.pyw'),
        ('bash', 'explorer.exe %s' % UI_WIN),
        ('bash', 'python -m reserve_ui'),
        # the live lock CLI's owner writes
        ('bash', 'python %s reserve whole --for 2h' % LIVE),
        ('ps', 'py %s reserve half --until 18:00' % LIVE_WIN),
        ('bash', 'pythonw %s release' % LIVE),
        ('bash', 'python %s release v-20261008T120000Z-abcd' % LIVE),
        ('bash', 'python %s stop r-1 --force' % LIVE),
        ('bash', 'python %s unbook b-1 --force' % LIVE),
        ('ps', 'Start-Process python -ArgumentList "%s reserve whole --for 1h"' % LIVE),
        ('ps', 'start python "%s release"' % LIVE),
        ('bash', '%s release' % LIVE),
        ('bash', 'python %s --test-root C:/tmp/x reserve whole --for 1h' % LIVE),
        ('bash', 'cmd //c python %s release' % LIVE),
        # the kit lock CLI's owner writes
        ('bash', 'python tools/machine_lock.py hold whole --for 1h'),
        ('ps', 'python tools\\machine_lock.py hold half --from 18:00 --until 20:00'),
        ('bash', 'python tools/machine_lock.py hold slots --cpu 0,1 --for 1h'),
        ('bash', 'python tools/machine_lock.py division --set "{}"'),
        ('bash', 'python tools/machine_lock.py division --set={}'),
        ('bash', 'python tools/machine_lock.py division --default'),
        ('bash', 'python tools/machine_lock.py game on'),
        ('ps', 'python tools/machine_lock.py game off'),
        ('bash', "python tools/machine_lock.py game set '[]'"),
        ('bash', 'python tools/machine_lock.py stop r-1 --force'),
        ('bash', 'python tools/machine_lock.py top r-1'),
        ('ps', 'python tools\\machine_lock.py --test-root C:/tmp/x top r-1'),
    ]

    ALLOWED = [
        # read-only and ordinary agent use of the live CLI
        ('bash', 'python %s status' % LIVE),
        ('ps', 'python %s status --json' % LIVE_WIN),
        ('bash', 'python %s status r-1' % LIVE),
        ('bash', 'python %s run --lock --kind engine-test --repo demo --slug wt -- python -m unittest' % LIVE),
        ('bash', 'python %s run --background --repo demo --slug wt -- python train.py' % LIVE),
        ('bash', 'python %s book --repo demo --slug wt --kind build --for 1h' % LIVE),
        ('bash', 'python %s unbook b-1' % LIVE),
        ('bash', 'python %s stop r-1' % LIVE),
        # and of the kit CLI
        ('bash', 'python tools/machine_lock.py hold agent --repo demo --slug wt --kind build --for 1h'),
        ('bash', 'python tools/machine_lock.py hold list'),
        ('bash', 'python tools/machine_lock.py hold end h-1'),
        ('bash', 'python tools/machine_lock.py division'),
        ('bash', 'python tools/machine_lock.py game'),
        ('bash', 'python tools/machine_lock.py game show'),
        ('bash', 'python tools/machine_lock.py background --repo demo --slug wt -- python train.py'),
        # reading reserve_ui and git on it, so work on the UI's own code stays open
        ('bash', 'cat %s' % UI),
        ('ps', 'type %s' % UI_WIN),
        ('ps', 'Get-Content %s -TotalCount 20' % UI_WIN),
        ('bash', 'rg -n "def " %s' % UI),
        ('bash', 'grep -n reserve ~/lock/reserve_ui.pyw'),
        ('bash', 'git add %s' % UI_REL),
        ('bash', 'git diff -- %s' % UI_REL),
        ('ps', 'git show HEAD:%s' % UI_REL),
        ('bash', 'git log --oneline -- %s' % UI_REL),
        ('bash', 'git commit --dry-run -m "port reserve_ui.pyw" -- %s' % UI_REL),
        # python -m of another module that only names reserve_ui
        ('bash', 'python -m py_compile %s' % UI_REL),
        ('ps', 'python -m pytest -k reserve_ui'),
        ('bash', 'python -m pyflakes %s' % UI),
        ('bash', 'python -um py_compile %s' % UI),
        ('bash', 'python -Bmpytest -k reserve_ui'),
        # the window package's tests, reads and checks
        ('bash', 'python -m unittest discover -s tests/lockui -t .'),
        ('ps', 'python -m unittest tests.lockui.test_window'),
        ('bash', 'python -m py_compile kitlib/lockui/window.py'),
        ('bash', 'cat kitlib/lockui/window.py'),
        ('bash', 'git diff -- kitlib/lockui/window.py'),
        # python reading its script, not stdin
        ('bash', 'cat %s | python tools/count_lines.py' % UI),
        # --force after -- is not the lock CLI's flag
        ('bash', 'python %s stop r-1 -- --force' % LIVE),
        ('bash', 'python %s run --lock --kind build --repo demo --slug wt -- tool stop --force' % LIVE),
        # top after -- is the wrapped command's, not the lock CLI's
        ('bash', 'python %s run --lock --kind build --repo demo --slug wt -- top -b -n 1' % LIVE),
        # unrelated commands that say "reserve", "release", "hold" or "game"
        ('bash', 'git log --oneline --grep reserve'),
        ('ps', 'python tools/backlog.py add --title "reserve whole slots for the owner"'),
        ('bash', 'python tools/reserve_check.py release'),
        ('bash', 'echo "machine_lock reserve whole"'),
        ('bash', 'python tools/release.py game on'),
        # the rule reads the file a command runs, never text that only names a tool
        ('bash', 'cat tools/machine_lock.py'),
        ('bash', 'grep -n "hold whole" tools/machine_lock.py'),
        ('ps', 'Select-String -Path tools/machine_lock.py -Pattern reserve'),
        ('bash', 'echo "python tools/machine_lock.py hold whole"'),
        ('bash', 'git log -p -- tools/machine_lock.py'),
        ('bash', 'python -c "import kitlib.lockui"'),
    ]

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def run_one(self, shell, text, sub, environ=None):
        make = bash if shell == 'bash' else ps
        if environ is None:
            environ = {} if sub else MARKED
        return decide(make(text, self.w.wt, sub=sub), self.w.ctx(environ=environ))

    def test_blocked(self):
        status = 'python "%s/tools/machine_lock.py" status' % W.fwd(self.w.kit_dir)
        for shell, text in self.BLOCKED:
            for sub in (True, False):
                with self.subTest(text, sub=sub):
                    d = self.run_one(shell, text, sub)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
                    self.assertIn(OWNER_ONLY, d.reason)
                    self.assertNotRegex(d.reason, r'\b[RQ]-\d+')
                    self.assertTrue(d.reason.endswith(' Instead: %s %s' % (
                        status, rules.tag('owner-tool', 'subagent' if sub else 'main'))), d.reason)

    def test_the_owner_session_may(self):
        # a main session nothing marks is the owner's own (docs/guard.md "Session mark")
        for shell, text in self.BLOCKED:
            with self.subTest(text):
                d = self.run_one(shell, text, False, environ={})
                self.assertIsNone(d, d and d.reason)

    def test_allowed(self):
        for shell, text in self.ALLOWED:
            with self.subTest(text):
                d = self.run_one(shell, text, True)
                self.assertIsNone(d, d and d.reason)

    # docs/guard.md "Fallback": a line the parser cannot read that names the lock CLI is refused
    # whole, its read verbs included, in every session but the owner's own
    UNCLEAN = [
        ('bash', 'eval "$c"; python tools/machine_lock.py status'),
        ('bash', 'eval "$c"; python %s status' % LIVE),
        ('bash', "echo 'x ; python tools/machine_lock.py hold list"),
        ('bash', 'eval "$c"; python -m tools.machine_lock status'),
        # the forms the parsed rule reads: -m ending a flag cluster, and a quoted module
        ('bash', 'eval "$c"; python -m machine_lock status'),
        ('bash', 'eval "$c"; python -Im machine_lock status'),
        ('bash', "eval \"$c\"; python -m 'machine_lock' status"),
        ('ps', 'iex $c; python -m "machine_lock" status'),
        ('ps', 'iex $c; python tools\\machine_lock.py status'),
    ]

    def test_an_unclean_line_naming_the_lock_cli_is_refused(self):
        for shell, text in self.UNCLEAN:
            for sub, environ in ((True, None), (False, None), (False, UNHELD)):
                with self.subTest(text, sub=sub, environ=environ):
                    d = self.run_one(shell, text, sub, environ)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
                    self.assertIn('could not read this command', d.reason)
                    self.assertIn('names the lock CLI', d.reason)
            with self.subTest(text, owner=True):
                d = self.run_one(shell, text, False, environ={})
                self.assertTrue(d is None or d.rule != 'owner-tool', d and d.reason)

    def test_an_unclean_line_running_an_owner_write_gives_the_write(self):
        d = self.run_one('bash', "echo 'x ; python tools/machine_lock.py hold whole --for 1h", True)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
        self.assertIn(OWNER_ONLY, d.reason)
        self.assertIn('machine_lock.py hold whole', d.reason)

    def test_an_unclean_line_naming_another_lock_file_is_not_held(self):
        for text in ('eval "$c"; python -m unittest tests/lock/test_machine_lock.py',
                     'eval "$c"; ls tools/machine_lock', 'eval "$c"; cat docs/machine_lock.pyw',
                     'eval "$c"; python -Im machine_locks status'):
            with self.subTest(text):
                d = self.run_one('bash', text, True)
                self.assertTrue(d is None or d.rule != 'owner-tool', d and d.reason)


class OwnerSetup(unittest.TestCase):
    """Machine setup's writes (the machine config's owner_email among them, plan decision 27) are the
    owner's to make: a session other than the owner's own that runs it to act is denied, its dry
    run is not, and an agent session's edit of the machine config is the write jail's."""

    KIT = 'C:/kit'
    BLOCKED = [
        ('bash', 'python tools/machine_setup.py --owner-email agent@example.invalid --apply'),
        ('bash', 'python C:/kit/tools/machine_setup.py --apply --owner-email a@example.invalid'),
        ('ps', 'py -3 C:\\kit\\tools\\machine_setup.py --apply'),
        ('bash', 'python3 tools/machine_setup.py --app'),
        ('bash', 'python tools/machine_setup.py --ap --owner-email a@example.invalid'),
        ('bash', 'python tools/machine_setup.py --repo ../demo --owner-email a@example.invalid '
                 '--agent-email b@example.invalid --apply'),
        ('bash', 'cd C:/kit && python tools/machine_setup.py --apply'),
        ('bash', 'python -m tools.machine_setup --apply'),
        ('ps', '& C:\\kit\\installer\\machine-setup.ps1 -OwnerEmail a@example.invalid -Apply'),
        ('ps', 'powershell -NoProfile -File C:\\kit\\installer\\machine-setup.ps1 -apply'),
        ('ps', 'powershell -File installer\\new-repo.ps1 -Target C:\\demo -OwnerEmail a@example.invalid '
               '-AgentEmail b@example.invalid -App'),
        ('ps', 'Start-Process python -ArgumentList "tools/machine_setup.py --apply"'),
    ]
    ALLOWED = [
        ('bash', 'python tools/machine_setup.py'),
        ('bash', 'python tools/machine_setup.py --owner-email a@example.invalid'),
        ('ps', '& C:\\kit\\installer\\machine-setup.ps1 -OwnerEmail a@example.invalid'),
        ('bash', 'python tools/machine_setup.py --help'),
        ('bash', 'cat tools/machine_setup.py'),
        ('bash', 'git diff -- tools/machine_setup.py kitlib/machinesetup/owner.py'),
        ('bash', 'python -m unittest discover -s tests/machinesetup -t .'),
        ('bash', 'python tools/backlog.py add --title "machine_setup.py --apply needs a note"'),
        ('bash', 'python tools/machine_setup.py --pm-settings x -- --apply'),
    ]

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def run_one(self, shell, text, sub, environ=None):
        make = bash if shell == 'bash' else ps
        if environ is None:
            environ = {} if sub else MARKED
        return decide(make(text, self.w.wt, sub=sub), self.w.ctx(environ=environ))

    def test_blocked(self):
        dry = 'python "%s/tools/machine_setup.py"' % W.fwd(self.w.kit_dir)
        for shell, text in self.BLOCKED:
            for sub, environ in ((True, None), (False, None), (False, UNHELD)):
                with self.subTest(text, sub=sub, environ=environ):
                    d = self.run_one(shell, text, sub, environ)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual(d.verdict, 'deny', d.reason)
                    if d.rule == 'unreadable':                 # a subagent's unclean line: refused whole
                        continue
                    self.assertEqual(d.rule, 'owner-tool', d.reason)
                    self.assertIn("only the owner runs it to act", d.reason)
                    self.assertTrue(d.reason.endswith(' Instead: %s %s' % (
                        dry, rules.tag('owner-tool', 'subagent' if sub else 'main'))), d.reason)

    # installer/machine-setup.sh, the Linux front end: run by a shell or as the program, sudo too
    SH_BLOCKED = [
        ('bash', 'sh installer/machine-setup.sh --owner-email a@example.invalid --apply'),
        ('bash', 'sudo sh /k/installer/machine-setup.sh --delegation --apply'),
        ('bash', 'sudo -u root bash -x -o pipefail installer/machine-setup.sh --pm-repo /p --apply'),
        ('bash', 'bash -- installer/machine-setup.sh --apply'),
        ('bash', './installer/machine-setup.sh --apply'),
        ('bash', '/bin/sh installer/machine-setup.sh --apply'),
    ]
    SH_ALLOWED = [
        ('bash', 'sh installer/machine-setup.sh --owner-email a@example.invalid'),
        ('bash', 'sudo sh installer/machine-setup.sh --delegation'),
        ('bash', 'sh -n installer/machine-setup.sh'),
        ('bash', 'cat installer/machine-setup.sh'),
        ('bash', 'bash tools/other.sh --apply'),
    ]

    def test_the_linux_installer_is_the_owners_to_apply(self):
        for shell, text in self.SH_BLOCKED:
            with self.subTest(text):
                d = self.run_one(shell, text, True)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
                self.assertIn('machine-setup.sh --apply', d.reason)
        for shell, text in self.SH_ALLOWED:
            with self.subTest(text):
                d = self.run_one(shell, text, True)
                self.assertTrue(d is None or d.rule != 'owner-tool', d and d.reason)

    def test_the_owner_session_may(self):
        for shell, text in self.BLOCKED:
            with self.subTest(text):
                d = self.run_one(shell, text, False, environ={})
                self.assertTrue(d is None or d.rule != 'owner-tool', d and d.reason)

    def test_allowed(self):
        for shell, text in self.ALLOWED:
            for sub in (True, False):
                with self.subTest(text, sub=sub):
                    d = self.run_one(shell, text, sub)
                    self.assertTrue(d is None or d.rule != 'owner-tool', d and d.reason)

    def test_code_that_only_names_the_setup_package_passes(self):
        """A python heredoc whose text names kitlib.machinesetup but imports nothing of it runs no
        machine setup: a subagent's run of it is allowed whole (observed refused as owner-setup)."""
        text = ("python - <<'EOF'\nimport pathlib\n"
                "t = pathlib.Path('docs/guard.md').read_text(encoding='utf-8')\n"
                "print(t.count('kitlib.machinesetup'))\nEOF")
        d = self.run_one('bash', text, True)
        self.assertIsNone(d, d and d.reason)

    def test_an_unread_line_that_runs_it_is_refused(self):
        text = "echo 'x ; python tools/machine_setup.py --owner-email a@example.invalid --apply"
        d = self.run_one('bash', text, False)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', 'owner-tool'), d.reason)
        self.assertIn('could not read this command', d.reason)
        self.assertIsNone(self.run_one('bash', text, False, environ={}))

    def test_an_agent_session_may_not_edit_the_machine_config(self):
        path = kuser.config_file(self.w.home)
        for sub, environ in ((True, {}), (False, MARKED)):
            with self.subTest(sub=sub):
                ev = event('Write', {'file_path': path, 'content': '{"owner_email": "a@x.io"}'}, self.w.wt, sub=sub)
                d = decide(ev, self.w.ctx(environ=environ))
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'), d.reason)


class NamedInHeredocText(TH.Hub):
    """A python edit script whose heredoc only names a protected file runs nothing of it and
    writes nothing there: it passes in every held session (seen refused as owner-setup and
    hub-secrets). A real run or write of each target is still refused."""

    @staticmethod
    def edit_script(path, old, new):
        return ("python - <<'EOF'\nimport pathlib\np = pathlib.Path(%r)\n"
                "t = p.read_text(encoding='utf-8')\nt = t.replace(%r, %r)\n"
                "p.write_text(t, encoding='utf-8')\nEOF" % (path, old, new))

    def test_an_edit_script_naming_machinesetup_passes(self):
        text = self.edit_script('kitlib/machinesetup/managed.py', 'kitlib.machinesetup.extras',
                                'kitlib.machinesetup.extras  # read by machine_setup.py --apply')
        self.allowed(lambda sub: bash(text, self.w.wt, sub))

    def test_an_edit_script_of_the_guard_s_hub_module_passes(self):
        text = self.edit_script('kitlib/guard/hub.py', "HUB_SERVER = 'hub'", "HUB_SERVER = 'hub'  # hub.py")
        self.allowed(lambda sub: bash(text, self.w.wt, sub))

    def test_machine_setup_run_to_act_is_still_refused(self):
        self.denied(lambda sub: bash('python tools/machine_setup.py --apply', self.w.wt, sub), 'owner-tool')

    def test_a_write_into_the_token_folder_is_still_refused(self):
        sec = W.fwd(self.secrets)
        self.denied(lambda sub: bash("python - <<'EOF'\nopen(%r, 'w').write('x')\nEOF" % (sec + '/owner.token'),
                                     self.w.wt, sub), 'hub-secrets')
        self.denied(lambda sub: bash('echo x > "%s/agent.token"' % sec, self.w.wt, sub), 'hub-secrets')


class PushBlockOff(unittest.TestCase):
    def test_no_push_rule_without_push_block(self):
        w = W.World(kit={'repo': {'name': 'demo'}})
        self.addCleanup(w.cleanup)
        self.assertIsNone(decide(bash('git push origin main', w.main), w.ctx()))

    def test_a_broken_kit_json_fails_closed(self):
        w = W.World(kit={'repo': {'no_such_key': 1}})
        self.addCleanup(w.cleanup)
        d = decide(bash('git push origin main', w.main), w.ctx())
        self.assertEqual(d.rule, 'git-push-main')
        self.assertIn('does not load', d.reason)
        self.assertIn('Instead: python -m json.tool "%s/.kit.json" ' % W.fwd(w.main), d.reason)


class PmDataNoVerify(unittest.TestCase):
    """git-push-pmdata: `git push --no-verify` skips the pre-push leak stage, so it is denied in a
    repository whose git common dir has kit-pm-data.json, in every session but the owner's own,
    whatever push_block says (it is off here). The file's existence decides for this rule; the
    record is a valid one, since the pm-data place rule reads it for linked worktrees."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'}})
        self.addCleanup(self.w.cleanup)
        common = os.path.join(self.w.main, '.git')
        os.makedirs(common, exist_ok=True)
        self.record = pmdata.write(common, 'pmdata', 'private', 'a' * 40) and pmdata.record_path(common)
        # with a record, the place rule reads a linked worktree's HEAD to tell the data worktree
        wt_git = os.path.join(common, 'worktrees', 'wt')
        os.makedirs(wt_git, exist_ok=True)
        with open(os.path.join(wt_git, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/kit/wt\n')

    def decide(self, text, sub=True, environ=None, cwd=None):
        return decide(bash(text, cwd or self.w.wt, sub=sub), self.w.ctx(environ=environ or {}))

    def assert_denied(self, d, instead=None):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-push-pmdata'), d.reason)
        if instead is not None:
            self.assertIn(' Instead: %s [kit guard:' % instead, d.reason)

    def test_a_subagent_and_a_marked_session_are_denied(self):
        plain = 'git -C "%s" push origin kit/wt' % W.fwd(self.w.wt)
        self.assert_denied(self.decide('git push --no-verify origin kit/wt'), plain)
        self.assert_denied(self.decide('git push --no-verify origin kit/wt', sub=False, environ=MARKED), plain)
        self.assert_denied(self.decide('git push --no-verify origin kit/wt', sub=False,
                                       environ={context.ROLE_ENV: 'pm'}), plain)
        # from the main checkout, a subagent's push with the hook runs in its worktree (write-jail
        # refuses the main checkout itself)
        self.assert_denied(self.decide('git -C %s push --no-verify private pmdata' % W.fwd(self.w.main)),
                           'git -C "%s" push private pmdata' % W.fwd(self.w.wt))

    def test_an_abbreviation_git_accepts_is_denied(self):
        for flag in ('--no-verif', '--no-ver', '--no-v'):
            with self.subTest(flag):
                self.assert_denied(self.decide('git push %s origin kit/wt' % flag),
                                   'git -C "%s" push origin kit/wt' % W.fwd(self.w.wt))

    def test_a_place_git_cannot_tell_is_denied(self):
        self.assert_denied(self.decide('git -C "%s" push --no-verify origin x' % W.fwd(self.w.unknown)))

    def test_the_owner_session_may(self):
        self.assertIsNone(self.decide('git push --no-verify origin kit/wt', sub=False))

    def test_from_the_pm_data_worktree_too(self):
        # the PM data place judges a push by two configs (rules.git_push_pm_data); each runs this rule
        self.w.user_config({'pm_data': {'path': self.w.data}})
        data_git = self.w.data_git_dir
        os.makedirs(data_git, exist_ok=True)
        with open(os.path.join(data_git, 'HEAD'), 'w', encoding='utf-8') as f:
            f.write('ref: refs/heads/pmdata\n')
        self.assertTrue(self.w.ctx().is_pm_data(self.w.data))
        pm = {context.ROLE_ENV: 'pm'}
        for text in ('git push --no-verify private pmdata', 'git push $F private pmdata'):
            with self.subTest(text):
                self.assert_denied(self.decide(text, sub=False, environ=pm, cwd=self.w.data))
                self.assert_denied(self.decide(text, cwd=self.w.data))
        self.assertIsNone(self.decide('git push --no-verify private pmdata', sub=False, cwd=self.w.data))
        self.assertIsNone(self.decide('git push private pmdata', sub=False, environ=pm, cwd=self.w.data))

    def test_without_a_record_or_without_no_verify_it_passes(self):
        self.assertIsNone(self.decide('git push origin kit/wt'))
        self.assertIsNone(self.decide('git push --verify origin kit/wt'))
        os.remove(self.record)
        self.assertIsNone(self.decide('git push --no-verify origin kit/wt'))
        self.assertIsNone(self.decide('git push --no-verify origin kit/wt', sub=False, environ=MARKED))

    COMPUTED = ('F=--no-verify; git push $F origin x', 'git push --n$F origin x', 'git $S origin x',
                'git push `echo --no-verify` origin x', 'git push $(echo --no-verify) origin x')

    def test_a_computed_push_word_is_denied(self):
        # the guard cannot tell what a computed word expands to, and --no-verify skips the hook
        unheld = UNHELD
        for text in self.COMPUTED:
            with self.subTest(text):
                d = self.decide(text, sub=False, environ=unheld)
                self.assert_denied(d, 'git -C "%s" push <remote> <branch>' % W.fwd(self.w.wt))
                self.assertIn('cannot judge a computed push word with PM data present', d.reason)
                self.assert_denied(self.decide(text))                       # a subagent
                self.assertIsNone(self.decide(text, sub=False))             # the owner's own
        d = decide(ps('git push $env:F origin x', self.w.wt), self.w.ctx(environ=unheld))
        self.assert_denied(d)
        self.assertIsNone(self.decide('git push origin x', sub=False, environ=unheld))     # literal

    XARGS = ("printf -- '--no-verify\\n' | xargs git push origin pmdata",
             'echo --no-verify | xargs -r git -C . push origin pmdata')
    BARE_XARGS = ('echo push --no-verify origin pmdata | xargs git',)

    def test_git_push_run_by_xargs_is_denied(self):
        # xargs appends its input to git's arguments: --no-verify, or the subcommand itself
        unheld = UNHELD
        for text in self.XARGS + self.BARE_XARGS:
            with self.subTest(text):
                d = self.decide(text, sub=False, environ=unheld)
                self.assert_denied(d)
                self.assertIsNotNone(self.decide(text))                     # a subagent
                d = self.decide(text, sub=False)                            # the owner's own
                self.assertNotEqual(d and d.rule, 'git-push-pmdata')
        # an unreadable line around it is read again
        d = self.decide("echo 'x ; echo push | xargs git", sub=False, environ=unheld)
        self.assert_denied(d)
        # xargs running another git subcommand is not a push
        self.assertIsNone(self.decide('echo a.txt | xargs git add --', sub=False, environ=unheld))

    def test_a_bare_xargs_git_is_denied_wherever_it_runs(self):
        # the input may name another repository (-C, --git-dir) and the subcommand: from a folder
        # with no record (no checkout at all here) the guard still cannot tell, so it denies
        unheld = UNHELD
        main = W.fwd(self.w.main)
        for text in ("printf '%%s\\n' -C %s push --no-verify origin pmdata | xargs git" % main,
                     "printf '%%s\\n' --git-dir=%s/.git push --no-verify origin pmdata | xargs git" % main,
                     'echo push --no-verify origin pmdata | xargs git'):
            for cwd in (self.w.plain, self.w.wt):
                with self.subTest(text, cwd=cwd):
                    d = self.decide(text, sub=False, environ=unheld, cwd=cwd)
                    self.assert_denied(d, 'git -C "<checkout>" <subcommand> <arguments>')
                    self.assertIn('cannot tell which repository or command xargs will run git with', d.reason)
                    self.assertIsNotNone(self.decide(text, cwd=cwd))        # a subagent
                    d = self.decide(text, sub=False, cwd=cwd)                # the owner's own
                    self.assertNotEqual(d and d.rule, 'git-push-pmdata')
        os.remove(self.record)                                              # machine-wide
        self.assert_denied(self.decide(self.BARE_XARGS[0], sub=False, environ=unheld, cwd=self.w.plain))
        self.assert_denied(self.decide("echo 'x ; echo push | xargs git", sub=False, environ=unheld, cwd=self.w.plain))
        self.assertIsNone(self.decide("echo 'x ; echo push | xargs git", sub=False, cwd=self.w.plain))
        self.assertIsNone(self.decide('echo a.txt | xargs git add --', sub=False, environ=unheld, cwd=self.w.plain))

    def test_git_push_run_by_xargs_without_a_record_is_unchanged(self):
        # with a literal subcommand the record of the repository decides
        os.remove(self.record)
        unheld = UNHELD
        for text in self.XARGS:
            with self.subTest(text):
                d = self.decide(text, sub=False, environ=unheld)
                self.assertNotEqual(d and d.rule, 'git-push-pmdata')

    def test_a_computed_push_word_without_a_record_is_unchanged(self):
        os.remove(self.record)
        unheld = UNHELD
        for text in self.COMPUTED:
            with self.subTest(text):
                self.assertIsNone(self.decide(text, sub=False, environ=unheld))
                d = self.decide(text)
                self.assertNotEqual(d and d.rule, 'git-push-pmdata')

    def test_send_pack_is_denied(self):
        # send-pack pushes without running the pre-push hook, in any form
        push = 'git -C "%s" push <remote> <branch>' % W.fwd(self.w.wt)
        unheld = UNHELD
        for text in ('git send-pack ../public.git refs/heads/pmdata',
                     'git -c x.y=1 send-pack --all ../public.git',
                     'git-send-pack ../public.git pmdata',
                     '"C:/Program Files/Git/mingw64/libexec/git-core/git-send-pack.exe" ../public.git pmdata'):
            with self.subTest(text):
                self.assert_denied(self.decide(text), push)
                self.assert_denied(self.decide(text, sub=False, environ=unheld), push)
                self.assertIsNone(self.decide(text, sub=False))           # the owner's own
        self.assert_denied(self.decide('git -C "%s" send-pack x pmdata' % W.fwd(self.w.unknown)))
        os.remove(self.record)
        self.assertIsNone(self.decide('git send-pack ../public.git pmdata'))
        self.assertIsNone(self.decide('git-send-pack ../public.git pmdata'))

    def test_send_pack_on_an_unreadable_line_is_read_again(self):
        unheld = UNHELD
        for text in ("echo 'x ; git send-pack ../public.git pmdata", "echo 'x ; git-send-pack ../public.git pmdata"):
            with self.subTest(text):
                d = self.decide(text, sub=False, environ=unheld)
                self.assert_denied(d)
                self.assertIn('could not read this command', d.reason)
                self.assertIsNone(self.decide(text, sub=False))
        os.remove(self.record)
        self.assertIsNone(self.decide("echo 'x ; git send-pack ../public.git pmdata", sub=False, environ=unheld))

    def test_an_unreadable_line_is_read_again(self):
        # the unclosed quote hides the push from the parse; the fallback pattern finds it
        text = "echo 'x ; git push --no-verify origin kit/wt"
        unheld = UNHELD
        d = self.decide(text, sub=False, environ=unheld)
        self.assert_denied(d)
        self.assertIn('could not read this command', d.reason)
        self.assertIsNone(self.decide(text, sub=False))                  # the owner's own
        os.remove(self.record)
        self.assertIsNone(self.decide(text, sub=False, environ=unheld))


class RolesFromConfig(unittest.TestCase):
    def test_a_lander_role_may_edit_main(self):
        w = W.World(kit={'repo': {'name': 'demo'}, 'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.addCleanup(w.cleanup)
        target = os.path.join(w.main, 'a.md')
        ev = event('Write', {'file_path': target, 'content': ''}, w.main, sub=True, agent_type='lander')
        self.assertIsNone(decide(ev, w.ctx()))
        ev = event('Write', {'file_path': target, 'content': ''}, w.main, sub=True, agent_type='worker')
        self.assertEqual(decide(ev, w.ctx()).rule, 'main-checkout')

    def test_a_lander_role_may_write_history_in_main(self):
        w = W.World(kit={'repo': {'name': 'demo'}, 'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.addCleanup(w.cleanup)
        for cmd in ('git merge --no-ff kit/wt', 'git commit -m "x"', 'git revert abc123'):
            self.assertIsNone(decide(event('Bash', {'command': cmd}, w.main, sub=True, agent_type='lander'), w.ctx()), cmd)
            self.assertEqual(decide(event('Bash', {'command': cmd}, w.main, sub=True, agent_type='worker'), w.ctx()).rule,
                             'git-main-history', cmd)
        self.assertEqual(decide(event('Bash', {'command': 'git restore a.py'}, w.main, sub=True, agent_type='lander'), w.ctx()).rule,
                         'git-restore')

    def test_the_plugin_prefixed_lander_is_the_lander(self):
        # docs/guard.md "Agent names": <plugin>:lander holds the lander role, no other spelling does
        p = lockmcp_names.PLUGIN_NAME + ':'           # kitlib.agentname.PREFIX
        w = W.World(kit={'repo': {'name': 'demo'}, 'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.addCleanup(w.cleanup)
        target = os.path.join(w.main, 'a.md')
        write = lambda kind: decide(event('Write', {'file_path': target, 'content': ''}, w.main, sub=True, agent_type=kind), w.ctx())
        git = lambda cmd, kind: decide(event('Bash', {'command': cmd}, w.main, sub=True, agent_type=kind), w.ctx())
        self.assertIsNone(write(p + 'lander'))
        for cmd in ('git merge --no-ff kit/wt', 'git commit -m "x"', 'git revert abc123'):
            self.assertIsNone(git(cmd, p + 'lander'), cmd)
        self.assertEqual(git('git restore a.py', p + 'lander').rule, 'git-restore')
        for kind in (p + 'worker', p + 'senior-worker', 'other:lander', p + p + 'lander', p.upper() + 'lander', p,
                     p + 'main', p.rstrip(':') + 'lander', 'x' + p + 'lander', p[1:] + 'lander'):
            with self.subTest(kind):
                self.assertEqual(write(kind).rule, 'main-checkout')
                self.assertEqual(git('git merge --no-ff kit/wt', kind).rule, 'git-main-history')


class MainCwdHint(unittest.TestCase):
    def test_a_command_writing_through_a_main_cwd_is_told_to_cd_first(self):
        # a subagent whose cwd is the main checkout runs python - or -c;
        # the refusal stays, its advice is the same run from the worktree (or a script by path)
        w = W.World()
        self.addCleanup(w.cleanup)
        for ev in (bash("python - <<'EOF'\nprint(1)\nEOF", w.main, sub=True),
                   ps('python -c "print(1)"', w.main, sub=True)):
            d = decide(ev, w.ctx())
            self.assertEqual(d and d.rule, 'unplaced', d and d.reason)
            self.assertIn('It writes through its working directory, which is the main checkout', d.reason)
            self.assertTrue(d.instead.startswith('cd "<worktree>"'), d.instead)
            self.assertNotIn('Make the change in a worktree of it', d.reason)
        # a redirect to a named file keeps the plain worktree advice
        d = decide(bash('python x.py > out.log 2>&1', w.main, sub=True), w.ctx())
        self.assertNotIn('It writes through its working directory', d.reason)


# python's code on the line, or read from standard input, counts as a write of the directory python
# runs in, whatever the code is: the guard does not read it (docs/guard.md "Shell writes")
PY_UNREAD = 'counted as written: the guard does not read what the code python is given writes'


class PythonCodeCountsAsWrite(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def judge(self, ev):
        d = decide(ev, self.w.ctx())
        return ('allow', None) if d is None else (d.verdict, d.rule)

    def test_python_c_read_only_program_in_main_checkout_counts_as_write(self):
        code = "import json,sys; print(json.load(open(sys.argv[1]))['a'])"
        for make, text in ((bash, 'python -I -c "%s" x.json' % code), (bash, 'python -I -c "print(1)"'),
                           (ps, 'python -I -c "print(1)"'), (bash, "py -3 -Ic 'print(1)'"),
                           (bash, 'python -c "import time; time.sleep(5)"')):
            with self.subTest(text):
                d = decide(make(text, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(d and (d.verdict, d.rule), ('deny', 'unplaced'), d and d.reason)
                self.assertIn(PY_UNREAD, d.reason)
                self.assertNotIn('sleep-wait', d.reason)

    def test_python_c_advice_is_cd_worktree_line(self):
        for make, text, instead in (
                (bash, 'python -c "print(1)"', 'cd "<worktree>" && python -c "print(1)"'),
                (bash, 'python3 -Bc \'print(1)\' a b', 'cd "<worktree>" && python3 -Bc \'print(1)\' a b'),
                (ps, 'python -c "print(1)"', 'cd "<worktree>"; if ($?) { python -c "print(1)" }'),
                (ps, '& "C:\\Py\\python.exe" -c "print(1)"',
                 'cd "<worktree>"; if ($?) { & "C:\\Py\\python.exe" -c "print(1)" }')):
            with self.subTest(text):
                d = decide(make(text, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(d and d.instead, instead, d and d.reason)
                self.assertIn('Read and Grep tools', d.reason)
                self.assertNotIn('python -I -c', d.reason)
                self.assertNotIn('worktree add', d.instead)

    def test_python_c_in_own_worktree_passes(self):
        for make, text in ((bash, 'python -c "open(\'x\', \'w\').write(\'y\')"'),
                           (ps, 'python -c "import os; os.remove(\'x\')"'),
                           (bash, "python - <<'EOF'\nopen('x', 'w')\nEOF")):
            with self.subTest(text):
                self.assertEqual(self.judge(make(text, self.w.wt, sub=True)), ('allow', None))

    def test_python_stdin_counts_as_write_of_cwd(self):
        sp = 'counted as written: Start-Process hands python a command line the guard does not read'
        for make, text, why in ((bash, 'echo "print(1)" | python -I -', PY_UNREAD),
                                (bash, "python - <<'EOF'\nprint(1)\nEOF", PY_UNREAD),
                                (bash, 'echo "print(1)" | python', PY_UNREAD), (ps, "'print(1)' | python -", PY_UNREAD),
                                (ps, "Start-Process python -ArgumentList '-c','print(1)'", sp)):
            with self.subTest(text):
                d = decide(make(text, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(d and (d.verdict, d.rule), ('deny', 'unplaced'), d and d.reason)
                self.assertTrue(d.reason.startswith('the directory `'), d.reason)
                self.assertIn(why, d.reason)
                self.assertEqual(self.judge(make(text, self.w.wt, sub=True)), ('allow', None))


class OtherTools(unittest.TestCase):
    def test_reads_and_unknown_tools_pass(self):
        w = W.World()
        self.addCleanup(w.cleanup)
        for ev in (event('Read', {'file_path': os.path.join(w.main, 'a')}, w.main, sub=True),
                   event('Glob', {'pattern': '**/*'}, w.main, sub=True),
                   bash('', w.main), event('Bash', {}, w.main)):
            self.assertIsNone(decide(ev, w.ctx()))


class PlainCommit(unittest.TestCase):
    """git verbs that make a commit in a linked worktree are commit.py's for every agent session
    (rules.git_plain_commit): plain git writes no Co-Authored-By trailer."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, text, environ=None, sub=False, shell=bash):
        return decide(shell(text, self.w.wt, sub), self.w.ctx(environ=environ or {}))

    def test_every_marked_session_is_held(self):
        for mark in ('coordinator', 'pm', 'deep-worker', 'consultant'):
            with self.subTest(mark):
                d = self.decide('git commit -m x -- a.py', {context.ROLE_ENV: mark})
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                self.assertIn('writes no Co-Authored-By trailer', d.reason)
                self.assertIn('/tools/commit.py" -C "%s" -m "<subject>" --model "<your model>" <each path you changed>' % W.fwd(self.w.wt),
                              d.reason)

    def test_an_unmarked_main_session_is_the_owners(self):
        for text in ('git commit -m x', 'git merge main', 'git revert HEAD', 'git am x.patch', 'git pull'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text))

    def test_a_nested_shell_is_read(self):
        d = self.decide("bash -c 'git commit -m x'", sub=True)
        self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)

    def test_the_main_checkout_keeps_its_own_rules(self):
        d = decide(bash('git commit -m x', self.w.main, True), self.w.ctx())
        self.assertEqual(d and d.rule, 'git-main-history', d and d.reason)

    def test_the_held_form_in_the_alternative_passes(self):
        for text in ('git revert HEAD~2', 'git cherry-pick abc123', 'git am fix.patch', 'git revert -m 1 HEAD',
                     'git cherry-pick --mainline=2 abc123', 'git revert --skip', 'git cherry-pick --skip'):
            with self.subTest(text):
                d = self.decide(text, sub=True)
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                held = d.instead.replace('<patch>', 'fix.patch')      # the placeholder is no shell word
                self.assertIsNone(self.decide(held, sub=True), held)

    def test_a_merge_or_pull_names_sync_base_with_the_worktree_and_the_typed_hash(self):
        """The Instead line is the exact command to run: this worktree, and the one hash the agent
        typed (rules.typed_hash); a branch name or two hashes leave <hash> for the coordinator's."""
        tool = 'python "%s/tools/sync_base.py" -C "%s" ' % (W.fwd(self.w.kit_dir), W.fwd(self.w.wt))
        for shell, text, commit in (
                (bash, 'git merge --no-edit d6454c8fa', 'd6454c8fa'),
                (ps, 'git merge -m "sync" 0123abcdef', '0123abcdef'),
                (bash, 'git pull origin d6454c8faa49', 'd6454c8faa49'),
                (bash, 'git merge origin/main', '<hash>'),
                (bash, 'git merge abc123', '<hash>'),
                (bash, 'git merge d6454c8fa 99fe8fadb', '<hash>')):
            with self.subTest(text):
                d = self.decide(text, sub=True, shell=shell)
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                self.assertEqual(d.instead, tool + '%s --model "<your model>"' % commit)

    def test_a_sequence_step_names_what_commit_py_leaves(self):
        for text in ('git cherry-pick --continue', 'git revert --continue', 'git am --continue'):
            with self.subTest(text):
                d = self.decide(text, sub=True)
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                self.assertIn('commits only the change in progress', d.reason)
                self.assertIn('--quit', d.reason)
        d = self.decide('git merge --continue', sub=True)
        self.assertNotIn('commits only the change in progress', d.reason)

    def test_a_skip_drops_the_change_in_progress(self):
        d = self.decide('git revert --skip', sub=True)
        self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
        self.assertIn('reset --merge', d.reason)
        self.assertNotIn('Commit with the kit tool', d.reason)

    def test_the_main_checkout_commit_all_names_no_lander_flag(self):
        d = decide(bash('git commit -am x', self.w.main), self.w.ctx())
        self.assertEqual(d and d.rule, 'git-commit-all', d and d.reason)
        self.assertNotIn('--lander-main-checkout', d.reason + d.instead)

    def test_scan(self):
        for sub, argv, makes in (
                ('commit', ['-m', 'x'], True), ('commit', ['--dry-run'], False), ('commit', ['--dry-r'], True),
                ('revert', ['-n', 'HEAD'], False), ('revert', ['-mn', 'HEAD'], True), ('revert', ['-nm', '1', 'HEAD'], False),
                ('revert', ['--no-commit', '--co', 'HEAD'], True), ('revert', ['--', '--no-commit'], True),
                ('merge', ['--no-commit', '--no-ff', 'main'], False), ('merge', ['--squash', '--no-squash'], True),
                ('merge', ['--ff-only', '--ff'], True), ('merge', ['--no-commit=x'], True),
                ('merge', ['-X', '--no-commit', 'main'], True), ('pull', ['--no-commit'], False),
                ('am', ['--abort'], False), ('am', ['--3way', 'x.patch'], True),
                ('revert', ['-n', '--commit', 'HEAD'], True), ('cherry-pick', ['-n', '--commit', 'x'], True),
                ('cherry-pick', ['-xn', '--comm', 'x'], True), ('merge', ['--mess', '--no-commit', 'main'], True),
                ('merge', ['--frob', 'main', '--no-commit'], True), ('revert', ['-Sn', 'HEAD'], True),
                ('cherry-pick', ['-Skey', '-n', 'x'], False), ('revert', ['-wn', 'HEAD'], True),
                ('revert', ['--no-edit', '--no-commit', 'HEAD'], False),
                ('merge', ['--no-ff', '--no-edit', '--no-commit', 'main'], False),
                ('merge', ['--no-commit', '--frob', 'main'], False), ('revert', ['-n', '--', 'HEAD'], False)):
            with self.subTest(sub=sub, argv=argv):
                cmd = cm.read('git %s %s' % (sub, ' '.join(argv)), sh.BASH, W.fwd(self.w.wt)).commands[0]
                self.assertEqual(rules.commit_scan(sub, cm.git_call(cm.invocation(cmd)).rest)[0], makes)

    def test_a_computed_word_after_a_hold_is_read_as_a_commit(self):
        # a known limit (rules.py, above COMMIT_VERBS): `$X` may be --commit, but is read as a commit
        cmd = cm.read('git cherry-pick -n $X', sh.BASH, W.fwd(self.w.wt)).commands[0]
        self.assertFalse(rules.commit_scan('cherry-pick', cm.git_call(cm.invocation(cmd)).rest)[0])

    def test_quit_advice_says_to_read_what_is_left_first(self):
        for text, where in (('git cherry-pick --skip', 'sequencer/todo'), ('git revert --continue', 'sequencer/todo'),
                            ('git am --skip', 'rebase-apply'), ('git am --continue', 'rebase-apply')):
            with self.subTest(text):
                d = self.decide(text, sub=True)
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                self.assertIn('--quit forgets what is left, so read it first', d.reason)
                self.assertIn('rev-parse --git-path %s' % where, d.reason)

    def test_am_options_are_gits(self):
        for argv, makes in ((['--show-current-patch=diff'], False), (['--show-current-patch=raw'], False),
                            (['--show-current-patch'], False), (['-Skey', '--abort'], False),
                            (['-S', '--abort'], False), (['--quoted-cr', 'strip', '--abort'], False),
                            (['--whitespace', 'fix', '--abort'], False), (['-mun', '--abort'], False),
                            (['--quoted-cr', '--abort'], True), (['-C', '--abort'], True),
                            (['x.patch'], True)):
            with self.subTest(argv=argv):
                cmd = cm.read('git am %s' % ' '.join(argv), sh.BASH, W.fwd(self.w.wt)).commands[0]
                self.assertEqual(rules.commit_scan('am', cm.git_call(cm.invocation(cmd)).rest)[0], makes)
        self.assertIsNone(self.decide('git am --show-current-patch=diff', sub=True))

    def test_held_form_keeps_strategy_and_drops_an_unplaced_value(self):
        wt = W.fwd(self.w.wt)
        for text, instead in (
                ('git cherry-pick -X theirs abc', 'git -C "%s" cherry-pick --no-commit -Xtheirs abc' % wt),
                ('git cherry-pick -Xours abc', 'git -C "%s" cherry-pick --no-commit -Xours abc' % wt),
                ('git revert --strategy ort --strategy-option=theirs HEAD',
                 'git -C "%s" revert --no-commit --strategy ort --strategy-option=theirs HEAD' % wt),
                ('git cherry-pick -m 1 -X theirs abc', 'git -C "%s" cherry-pick --no-commit -m 1 -Xtheirs abc' % wt),
                ('git cherry-pick --frob val abc', 'git -C "%s" cherry-pick --no-commit <commit>' % wt),
                ('git revert -w HEAD', 'git -C "%s" revert --no-commit <commit>' % wt),
                ("git cherry-pick -X 'a;b' abc", 'git -C "%s" cherry-pick --no-commit \'-Xa;b\' abc' % wt),
                ("git cherry-pick -X 'a b' abc", 'git -C "%s" cherry-pick --no-commit \'-Xa b\' abc' % wt),
                ('git revert --strategy-option "it\'s" HEAD',
                 'git -C "%s" revert --no-commit --strategy-option <value> HEAD' % wt),
                ('git cherry-pick "-Xit\'s" abc', 'git -C "%s" cherry-pick --no-commit -X<value> abc' % wt),
                ('git revert "--strategy-option=it\'s" HEAD',
                 'git -C "%s" revert --no-commit --strategy-option=<value> HEAD' % wt),
                ('git cherry-pick -X "$s" abc', 'git -C "%s" cherry-pick --no-commit -X<value> abc' % wt),
                ('git revert --strategy $s HEAD', 'git -C "%s" revert --no-commit --strategy <value> HEAD' % wt),
                ('git cherry-pick -X a,b abc', 'git -C "%s" cherry-pick --no-commit \'-Xa,b\' abc' % wt),
                ('git revert --strategy @s HEAD', 'git -C "%s" revert --no-commit --strategy \'@s\' HEAD' % wt)):
            with self.subTest(text):
                d = self.decide(text, sub=True)
                self.assertEqual(d and d.rule, 'git-plain-commit', d and d.reason)
                self.assertEqual(d.instead, instead)


# the kit's own tools, as whole names (after a path separator, a quote or a space), and any
# other script file name: a refusal of git that code runs may name only the first
KIT_TOOL_NAMES = re.compile(r'(?<![\w.-])(commit|sync_base)\.py(?!\w)')
SCRIPT_NAME = re.compile(r'(?i)\.(py|pyw|js|mjs|cjs|ts|ps1|sh|bat|cmd)\b')


class InterpreterGit(unittest.TestCase):
    """git that python or node code given on the line runs is refused for every agent session
    (rules.interpreter_git): the guard reads git only as a typed shell command."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, text, cwd=None, environ=None, sub=True, shell=bash):
        return decide(shell(text, cwd or self.w.wt, sub), self.w.ctx(environ=environ or {}))

    def assert_refused(self, d, text):
        self.assertEqual(d and (d.verdict, d.rule), ('deny', 'interpreter-git'), d and d.reason)
        self.assertIn('reads git only as a typed shell command', d.reason)
        self.assertIn('commit.py', d.reason)
        self.assertIn('sync_base.py', d.reason)
        self.assertIn('do not route it through a program or a script file', d.reason)
        # neither the advice nor the Instead may name a script file but the kit's own tools: a
        # refused git command must never be routed through one
        for said in (d.reason.split(' runs git from ', 1)[1], d.instead):
            self.assertNotRegex(KIT_TOOL_NAMES.sub('', said), SCRIPT_NAME, said)

    def test_the_script_check_strips_only_the_kit_tools(self):
        self.assertEqual(KIT_TOOL_NAMES.sub('', 'python "x/tools/commit.py" and sync_base.py'), 'python "x/tools/" and ')
        for name in ('my_commit.py', 'recommit.py', 'x-sync_base.py', 'commit.py.bak.py'):
            with self.subTest(name):
                self.assertRegex(KIT_TOOL_NAMES.sub('', name), SCRIPT_NAME)

    def test_code_running_git_is_refused(self):
        wt = W.fwd(self.w.wt)
        for shell, text, instead in (
                (bash, "python - <<'EOF'\nimport subprocess\nsubprocess.run(['git','-C',w,'update-ref',r,c])\nEOF",
                 'git -C "%s" <the git command your code ran>' % wt),
                (bash, 'py -3 -c "import os; os.system(\'git push\')"', None),
                (bash, 'python3 <<< "import subprocess; subprocess.run([\'git\', \'status\'])"', None),
                (bash, 'echo "import subprocess; subprocess.run([\'git\',\'push\'])" | python -', None),
                (ps, "@'\nimport subprocess\nsubprocess.run(['git','push'])\n'@ | python -", None),
                (bash, 'node -e "require(\'child_process\').execSync(\'git branch -D x\')"', None),
                (bash, 'node -r x -e "require(\'child_process\').execSync(\'git commit -m y\')"',
                 rules.kit_tool(self.w.ctx(), 'commit')),
                (bash, 'node --require x --import y -e "require(\'child_process\').execSync(\'git push\')"',
                 None),
                (bash, 'node -pe "require(\'child_process\').execSync(\'git commit -m x\')"',
                 rules.kit_tool(self.w.ctx(), 'commit')),
                (bash, 'node --loader a --experimental-loader b -C c --conditions d --input-type module '
                       '-e "require(\'child_process\').execSync(\'git push\')"', None),
                (bash, 'python -c "import subprocess; subprocess.run([r\'C:/Program Files/Git/cmd/git.exe\', \'st\'])"',
                 None),
                (bash, 'python -c "import subprocess as s; s.run(f\'git -C {w} cherry-pick {c}\', shell=True)"',
                 rules.kit_tool(self.w.ctx(), 'commit')),
                (bash, 'python -c "import shutil, subprocess; subprocess.run([shutil.which(\'git\'), \'log\'])"', None),
                (bash, 'python -c "import subprocess; subprocess.run([\'git\', \'rev-parse\', \'HEAD\'])"', None)):
            with self.subTest(text):
                d = self.decide(text, shell=shell)
                self.assert_refused(d, text)
                if instead is not None:
                    self.assertIn(instead, d.instead)

    def test_every_marked_session_is_held_and_the_owner_is_not(self):
        text = 'python -c "import subprocess; subprocess.run([\'git\', \'status\'])"'
        for mark in ('coordinator', 'deep-worker', 'consultant', 'pm'):
            with self.subTest(mark):
                self.assert_refused(self.decide(text, environ={context.ROLE_ENV: mark}, sub=False), text)
        self.assertIsNone(self.decide(text, sub=False))

    def test_any_directory_is_held(self):
        d = self.decide('python -c "import subprocess; subprocess.run([\'git\', \'status\'])"', cwd=self.w.temp)
        self.assert_refused(d, 'temp')
        self.assertEqual(d.instead, 'git -C "<worktree>" <the git command your code ran>')

    def test_code_that_runs_no_git_passes(self):
        for text in ('python -c "print(\'git\')"',
                     'git log | python -c "import sys,subprocess; subprocess.run([\'python\',\'x.py\'], '
                     'input=sys.stdin.read())"',
                     'git show HEAD:x.py | python -',
                     'python -c "import subprocess; subprocess.run([\'python\', \'-V\'])"',
                     'python -c "import subprocess; subprocess.run([\'gitk\'])"',
                     'python tools/x.py', 'python -m json.tool x.json'):
            with self.subTest(text):
                self.assertIsNone(self.decide(text))

    def test_the_main_checkout_keeps_unplaced(self):
        d = self.decide('python -c "import subprocess; subprocess.run([\'git\', \'status\'])"', cwd=self.w.main)
        self.assertEqual(d and d.rule, 'unplaced', d and d.reason)


if __name__ == '__main__':
    unittest.main()
