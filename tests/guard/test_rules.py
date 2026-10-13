"""kitlib.guard.decide over PreToolUse-shaped payloads (docs/guard.md "Rules"): main session and
subagent, Bash and PowerShell, allow and deny, and the exact alternative each deny names.

Each row: (name, payload builder, expected kind or None for allow, rule, the text after
"Instead: "). Paths in the expected text are filled from the fake world ({main}, {wt}, ... in
forward-slash form; {main_bs} keeps backslashes)."""
import json
import os
import unittest

from kitlib import lockmcp as lockmcp_names, pmdata
from kitlib.config import user as kuser
from kitlib.git import repo
from kitlib.guard import context, rules
from kitlib.lockmcp import server as lockmcp

from . import world as W
from .claudehost import MCP_PREFIX, decide
from .extra_rows import ROWS as EXTRA_ROWS
from .review_rows import ROWS as REVIEW_ROWS
from .shell_rows import LOCK, ROWS as SHELL_ROWS
from .world import bash, event, ps

ROWS = [
    # ---------------------------------------------------------------- wrapped gate (P16)
    ('gate piped to tail', lambda w: bash('python tools/gate.py --full 2>&1 | tail -5', w.wt),
     'deny', 'wrapped-gate',
     'python tools/gate.py --full > "{temp}/wt-gate.log" 2>&1'),
    ('cd then gate piped (P16 shape), subagent', lambda w: bash('cd %s && python tools/gate.py 2>&1 | tail -n 40' % W.fwd(w.wt), w.main, sub=True),
     'deny', 'wrapped-gate',
     'python tools/gate.py > "{temp}/wt-gate.log" 2>&1'),
    ('lock run piped, PowerShell', lambda w: ps('python ~/.claude/tools/machine_lock/machine_lock.py run --lock --kind engine-test --repo demo --slug wt -- python -m unittest 2>&1 | Select-Object -Last 20', w.wt),
     'deny', 'wrapped-gate',
     'python ~/.claude/tools/machine_lock/machine_lock.py run --lock --kind engine-test --repo demo --slug wt -- python -m unittest > "{temp}/wt-gate.log" 2>&1'),
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
     LOCK + ' --kind engine-run --repo demo --slug wt -- godot --headless --path .'),
    ('godot.exe by path, PowerShell', lambda w: ps('& "C:\\Program Files\\Godot\\godot.exe" --headless', w.main, sub=True),
     'deny', 'heavy', LOCK + ' --kind engine-run --repo demo --slug main -- "C:\\Program Files\\Godot\\godot.exe" --headless'),
    ('env flag alone is no lock', lambda w: bash('MACHINE_LOCK_RUN=r-20261006T120000Z-abcdef:1:2 godot -s x.gd', w.wt),
     'deny', 'heavy', LOCK + ' --kind engine-run --repo demo --slug wt -- godot -s x.gd'),
    ('godot nested in bash -c', lambda w: bash('bash -c "godot --headless"', w.wt), 'deny', 'heavy',
     LOCK + ' --kind engine-run --repo demo --slug wt -- godot --headless'),
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
    ('git in a place git cannot tell', lambda w: bash('git checkout x', w.unknown), 'deny', 'git-checkout', None),
    ('git after a cd it cannot follow', lambda w: bash('cd "$WT" && git stash', w.wt), 'deny', 'git-stash', None),
    ('restore after a cd it cannot follow', lambda w: bash('cd "$WT" && git restore a.py', w.wt), 'deny', 'git-restore', None),
    ('subagent commit after a cd it cannot follow', lambda w: bash('cd "$WT" && git commit -m x', w.wt, sub=True), 'deny', 'git-main-history', None),
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
    ('restore in a worktree', lambda w: bash('git restore a.py', w.wt), None, None, None),
    ('subagent commit in main', lambda w: bash('git commit -m "two words"', w.main, sub=True), 'deny', 'git-main-history',
     'git -C "{main}" worktree add .claude/worktrees/<slug> -b <branch>'),
    ('subagent commit in main via -C from its worktree', lambda w: bash('git -C %s commit -m "two words"' % W.fwd(w.main), w.wt, sub=True),
     'deny', 'git-main-history', 'git -C "{wt}" commit -m "two words"'),
    ('subagent merge in main', lambda w: ps('git merge kit/wt', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent rebase in main', lambda w: bash('git rebase origin/main', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent cherry-pick in main', lambda w: bash('git cherry-pick abc123', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent revert in main', lambda w: bash('git revert abc123', w.main, sub=True), 'deny', 'git-main-history', None),
    ('subagent am in main', lambda w: bash('git am x.patch', w.main, sub=True), 'deny', 'git-main-history', None),
    ('main-role commit in main', lambda w: bash('git commit -m "x"', w.main), None, None, None),
    ('subagent commit in its worktree', lambda w: bash('git commit -m "x"', w.wt, sub=True), None, None, None),
    ('subagent log in main', lambda w: bash('git log -1', w.main, sub=True), None, None, None),

    # ---------------------------------------------------------------- push to main (push_block on)
    ('push main with no record', lambda w: bash('git push origin main', w.main), 'deny', 'git-push-main',
     'python tools/land.py <branch> --expect-main %s --expect-tip <branch tip sha>' % W.SHA_REMOTE_MAIN),
    ('push HEAD:main from a worktree', lambda w: bash('git push origin HEAD:main', w.wt), 'deny', 'git-push-main',
     'python tools/land.py kit/wt --expect-main %s --expect-tip %s' % (W.SHA_REMOTE_MAIN, W.SHA_WT)),
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
     'deny', 'main-checkout', 'git -C "{main}" worktree add .claude/worktrees/<slug> -b <branch>'),
    ('subagent NotebookEdit in main', lambda w: event('NotebookEdit', {'notebook_path': os.path.join(w.main, 'n.ipynb'), 'new_source': ''}, w.wt, sub=True),
     'deny', 'main-checkout', None),
    ('subagent Edit in its worktree', lambda w: event('Edit', {'file_path': os.path.join(w.wt, 'a.py'), 'old_string': 'a', 'new_string': 'b'}, w.wt, sub=True),
     None, None, None),
    ('subagent Edit where git cannot tell', lambda w: event('Edit', {'file_path': os.path.join(w.unknown, 'a.py'), 'old_string': 'a', 'new_string': 'b'}, w.wt, sub=True),
     'deny', 'main-checkout', None),
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
    ('subagent write to a variable path', lambda w: ps('"x" > $env:OUT', w.wt, sub=True), None, None, None),
    ('subagent redirect in its worktree', lambda w: bash('echo hi > notes.txt', w.wt, sub=True), None, None, None),
    ('subagent redirect in a heredoc body is data', lambda w: bash("cat > notes.txt <<'EOF'\nx > %s/y.txt\nEOF" % W.fwd(w.main), w.wt, sub=True),
     None, None, None),
    ('main session redirect into main', lambda w: bash('echo hi > out.txt', w.main), None, None, None),

    # ---------------------------------------------------------------- unreadable lines (fallback)
    ('unclosed quote hiding a checkout', lambda w: bash("echo 'x ; git checkout main", w.main), 'deny', 'git-checkout', None),
    ('unclosed quote in a worktree', lambda w: bash("echo 'x ; git checkout main", w.wt), None, None, None),
    ('unclosed quote hiding a restore', lambda w: bash("echo 'x ; git restore a.py", w.main), 'deny', 'git-restore', None),
    ('unclosed quote hiding restore --staged', lambda w: bash("echo 'x ; git restore --staged a.py", w.main), None, None, None),
    ('unclosed quote hiding restore -SW', lambda w: bash("echo 'x ; git restore -SW a.py", w.main), 'deny', 'git-restore', None),
    ('unclosed quote hiding a subagent merge', lambda w: bash("echo 'x ; git merge kit/wt", w.main, sub=True), 'deny', 'git-main-history', None),
    ('unclosed quote hiding a main-role merge', lambda w: bash("echo 'x ; git merge kit/wt", w.main), 'deny', 'unreadable',
     "echo 'x ; git merge kit/wt'"),
    ('unclosed quote hiding a subagent merge in its worktree', lambda w: bash("echo 'x ; git merge kit/wt", w.wt, sub=True), 'deny', 'unreadable', None),
    ('unclosed quote hiding a checkout, real alternative', lambda w: bash("echo 'x ; git checkout main", w.main), 'deny', 'git-checkout',
     'git -C "{main}" worktree add .claude/worktrees/<slug> <branch>'),
    ('cmd /c with a trailing backslash root', lambda w: bash('cmd /c "dir /s C:\\"', w.wt), 'deny', 'root-search', None),
    ('variable program running godot', lambda w: bash('GODOT=/c/tools/godot.exe; $GODOT --headless', w.wt), 'deny', 'heavy',
     LOCK + ' --kind engine-run --repo demo --slug wt -- <the program $GODOT names> --headless'),
] + SHELL_ROWS + REVIEW_ROWS + EXTRA_ROWS

# An alternative is a command to run as written: no closing full stop, no leading prose word
# (docs/guard.md "Rules"). The prose goes in its own sentence before "Instead:".
PROSE_WORDS = ('run', 'work', 'make', 'read', 'write', 'commit', 'merge', 'name', 'nothing', 'fix', 'ask', 'the',
               'as', 'from', 'reproduce', 'approve', 'stage', 'search', 'use')


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
        self.alt = ('python tools/ledger.py add --repo <repo> --prov <prov> '
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
        d = kuser.config_dir(self.w.home)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'config.json'), 'wb') as f:
            f.write(raw)

    def test_a_broken_machine_config_denies_every_subagent_write(self):
        target = os.path.join(self.w.wt, 'a.py')
        for raw, why in ((b'{', 'not valid JSON'), (b'{"ledger_path": "' + bytes([0xff]) + b'"}', 'cannot read'),
                         (b'[]', 'must be an object'), (b'{"ledger_path": "rulings.md"}', 'absolute'),
                         (b'{"ledger_path": 5}', 'ledger_path')):
            with self.subTest(raw):
                self.write_config(raw)
                for ev in (event('Write', {'file_path': target, 'content': ''}, self.w.wt, sub=True),
                           event('Edit', {'file_path': target, 'old_string': 'a', 'new_string': 'b'}, self.w.wt, sub=True),
                           bash('echo x > notes.txt', self.w.wt, sub=True)):
                    d = decide(ev, self.w.ctx())
                    self.assertIsNotNone(d, ev['tool_name'])
                    self.assertEqual(d.rule, 'ledger', d.reason)
                    self.assertIn('The owner or the PM session fixes it. Instead: python -m json.tool "%s/%s/config.json" '
                                  % (W.fwd(self.w.home), kuser.DIR_NAME), d.reason)
                    self.assertIn(why, d.reason)
                # the main session and reads stay open
                self.assertIsNone(decide(event('Write', {'file_path': target, 'content': ''}, self.w.wt), self.w.ctx()))
                self.assertIsNone(decide(bash('git status', self.w.wt, sub=True), self.w.ctx()))

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
            self.assertIn('Instead: git -C "%s" reset --keep HEAD' % W.fwd(self.w.data), d.reason)
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
        self.assertIsNone(decide(bash('git commit -m x', self.w.wt, sub=True), self.w.ctx()))

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
    """reserve_ui.pyw and the lock CLI's owner-only writes are the owner's to run himself; an agent's
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
        ('bash', 'python -c "import runpy; runpy.run_path(\'reserve_ui.pyw\')"'),
        # the window's package, started from python -c
        ('bash', 'python -c "from kitlib.lockui import window; window.main()"'),
        ('ps', 'pythonw -c "import kitlib.lockui.window as w; w.main()"'),
        ('bash', 'python -c "from kitlib import lockui"'),
        ('bash', 'python -Sc "from kitlib.lockui.window import main; main()"'),
        ('bash', 'p=%s; pythonw "$p"' % UI),
        ('ps', '$ui = "%s"; & $ui' % UI_WIN),
        ('ps', '$p = "reserve_ui.pyw"; pythonw $p'),
        ('bash', 'pyw reserve_ui.pyw'),
        ('ps', 'pyw -3 reserve_ui.pyw'),
        ('ps', 'pyw.exe %s' % UI_WIN),
        ('bash', 'py -u reserve_ui.pyw'),
        ('bash', 'start /b pythonw reserve_ui.pyw'),
        ('ps', 'cmd /c start /b pythonw reserve_ui.pyw'),
        ('ps', 'explorer reserve_ui.pyw'),
        ('bash', 'explorer.exe %s' % UI_WIN),
        ('bash', 'python -m reserve_ui'),
        # combined short flags: -m or -c at the end of a cluster, or a module joined to -m
        ('bash', 'python -um runpy reserve_ui.pyw'),
        ('ps', 'python -Bm runpy %s' % UI_WIN),
        ('bash', 'python -Im reserve_ui'),
        ('bash', 'python -mrunpy reserve_ui.pyw'),
        ('bash', 'python -umrunpy reserve_ui.pyw'),
        ('bash', 'python -Sc "import runpy; runpy.run_path(\'reserve_ui.pyw\')"'),
        ('bash', 'mod=reserve_ui; python -m $mod'),
        ('ps', '$m = "runpy"; python -um $m reserve_ui.pyw'),
        ('bash', 'python -m runpy %s' % UI),
        ('bash', 'python < reserve_ui.pyw'),
        ('bash', 'python - < %s' % UI),
        ('bash', 'cat reserve_ui.pyw | python'),
        ('bash', 'cat %s | pythonw -' % UI),
        ('ps', 'Get-Content reserve_ui.pyw | python'),
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
        ('bash', 'python %s run --lock --kind build --repo demo --slug wt -- python %s reserve whole --for 1h'
         % (LIVE, LIVE)),
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
        ('bash', 'python %s run --lock --kind build --repo demo --slug wt -- python tools/machine_lock.py top r-1'
         % LIVE),
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
        ('bash', 'git commit -m "port reserve_ui.pyw" -- %s' % UI_REL),
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
                    self.assertEqual((d.verdict, d.rule), ('deny', 'owner-lock'), d.reason)
                    self.assertIn(OWNER_ONLY, d.reason)
                    self.assertNotRegex(d.reason, r'\b[RQ]-\d+')
                    self.assertTrue(d.reason.endswith(' Instead: %s %s' % (
                        status, rules.tag('owner-lock', 'subagent' if sub else 'main'))), d.reason)

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

    def _first(self, text):
        from kitlib.guard import commands as cm, shell as sh
        p = cm.read(text, sh.BASH, self.w.wt)
        self.assertFalse(p.clean)
        self.assertEqual(len(p.commands), 1, [c.words for c in p.commands])
        c = p.commands[0]
        return c, cm.invocation(c), self.w.wt, 'main'

    def test_unread_line_rereads_with_owner_lock(self):
        # an unclosed quote hides the command inside echo's argument, so only the fallback sees it:
        # it re-reads the text from the wrapped-gate match and runs the parsed rules on it
        text = ("echo 'x ; python %s run --lock --kind build --repo demo --slug wt -- python %s reserve whole"
                " --for 1h | tee out.log" % (LIVE, LIVE))
        self.assertIsNone(rules.owner_lock(None, self.w.ctx(environ=MARKED), *self._first(text)), 'the parse saw it')
        for sub in (True, False):
            with self.subTest(sub=sub):
                d = self.run_one('bash', text, sub)
                self.assertIsNotNone(d, 'allowed')
                self.assertEqual((d.verdict, d.rule), ('deny', 'owner-lock'), d.reason)
                self.assertIn('could not read this command', d.reason)
                self.assertIn(OWNER_ONLY, d.reason)


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
        self.assert_denied(self.decide('git -C %s push --no-verify private pmdata' % W.fwd(self.w.main)),
                           'git -C "%s" push private pmdata' % W.fwd(self.w.main))

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


class OtherTools(unittest.TestCase):
    def test_reads_and_unknown_tools_pass(self):
        w = W.World()
        self.addCleanup(w.cleanup)
        for ev in (event('Read', {'file_path': os.path.join(w.main, 'a')}, w.main, sub=True),
                   event('Glob', {'pattern': '**/*'}, w.main, sub=True),
                   bash('', w.main), event('Bash', {}, w.main)):
            self.assertIsNone(decide(ev, w.ctx()))


if __name__ == '__main__':
    unittest.main()
