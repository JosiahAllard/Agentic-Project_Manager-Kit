"""Every Instead the guard prints passes the guard for the same role (docs/guard.md "Rules": an alternative is a command to run as written). Each refusal's Instead is
run again with the refused call's payload (its session, agent and folder; a file tool's call as a
Bash one) and context, and must be allowed. An Instead with a placeholder (`<worktree>`,
`<subject>`) is a form to fill, not a command: the test_rules rows skip it; the table below fills
the ones it names. A bare `cd <folder>` does nothing, so it counts as a failure.

KNOWN_FAILS names each form that is refused again today, with why; it is checked both ways, so a
new failing form fails the test and so does a known one that now passes (take it off the list).
DecisionPins pins the refusal itself for every input whose Instead this branch changed: the same
(verdict, rule) before and after (the own-pull rows are pinned in
test_backlog_role.OwnPullDecision)."""
import copy
import os
import re
import unittest

from kitlib import pmdata
from kitlib.guard import context, rules

from . import world as W
from .claudehost import decide
from .test_jail import JailWorld
from .test_rules import ROWS
from .world import bash, ps

PM = {context.ROLE_ENV: 'pm'}
PLACEHOLDER = re.compile(r'<[^<>\s][^<>]*>')
BARE_CD = re.compile(r'^cd\s+("[^"]*"|\S+)\s*$')

# A main-checkout or write-jail rewrite that leaves the target in place: an attached writer
# option's value (dd of=, curl --output=, curl --output-dir, gawk -d"..", ruby -C, cp -t"..") is not
# retargeted, so the same write is refused again.
ATTACHED = 'the rewrite does not retarget an attached writer option; the same write is refused again'
KNOWN_FAILS = {
    # test_rules.ROWS (shell_rows, review_rows)
    'dd of= into main': ATTACHED,
    'curl --output= into main': ATTACHED,
    'curl -o --output-dir into main': ATTACHED,
    'find -execdir rm of main from a worktree, subagent':
        'the -execdir rm keeps its {} target, which the guard cannot place (unplaced)',
    # CASES below
    'gawk -d into another folder': ATTACHED,
    'ruby -C into another folder': ATTACHED,
    'cp -t into another folder': ATTACHED,
    'git branch -D backlog on a branch store':
        'git branch -d of the store branch is a raw write of the backlog store (backlog-raw-write)',
    'git push of the PM data branch':
        'git -C <main checkout> push is outside a subagent\'s write jail (write-jail)',
}

FILL = {'<subject>': 'x', '<what>': 'x', '<each path you changed>': 'a.txt'}


def instead_of(d):
    return d.instead


def run_again(ev, text):
    ev2 = copy.deepcopy(ev)
    if ev2.get('tool_name') not in ('Bash', 'PowerShell'):
        ev2['tool_name'] = 'Bash'
    ev2['tool_input'] = {'command': text, 'description': 'test'}
    return ev2


def again(ev, text, ctx):
    """None when `text` passes, run as `ev` was; else why not."""
    if BARE_CD.match(text):
        return 'a bare cd runs nothing'
    d = decide(run_again(ev, text), ctx)
    return None if d is None else '%s: %s' % (d.rule, d.reason)


def jail_world():
    w = JailWorld()
    w.git.ancestors.add((W.SHA_WT, 'main'))
    return w


def branch_store_world():
    return W.World(kit={'repo': {'name': 'demo', 'push_block': True}, 'backlog': {'store': 'branch'}})


def pmdata_world():
    w = W.World(kit={'repo': {'name': 'demo'}})
    common = os.path.join(w.main, '.git')
    os.makedirs(common, exist_ok=True)
    pmdata.write(common, 'pmdata', 'private', 'a' * 40)
    wt_git = os.path.join(common, 'worktrees', 'wt')        # as test_pmdata_gaps: the worktree's HEAD
    os.makedirs(wt_git, exist_ok=True)
    with open(os.path.join(wt_git, 'HEAD'), 'w', encoding='utf-8') as fh:
        fh.write('ref: refs/heads/kit/wt\n')
    return w


def f(path):
    return W.fwd(path)


# (name, world, payload builder, environ, the (verdict, rule) of the refusal). Run from the
# worktree as a subagent unless the builder says otherwise.
CASES = [
    # fix 2: a git write into a read-only or add-only data root is the same git in the worktree
    ('git merge-tree in a read-only data root, bash', jail_world,
     lambda w: bash('git -C "%s" merge-tree --write-tree a b' % f(w.ro), w.wt, True), None, 'write-jail'),
    ('git merge-tree in a read-only data root, PowerShell', jail_world,
     lambda w: ps('git -C "%s" merge-tree --write-tree a b' % f(w.ro), w.wt, True), None, 'write-jail'),
    ('git merge-tree in an add-only data root, bash', jail_world,
     lambda w: bash('git -C "%s" merge-tree --write-tree a b' % f(w.add), w.wt, True), None, 'write-jail'),
    ('git merge-tree in an add-only data root, PowerShell', jail_world,
     lambda w: ps('git -C "%s" merge-tree --write-tree a b' % f(w.add), w.wt, True), None, 'write-jail'),
    # a commit or a shared stash entry retargeted to the worktree: the kit's commit tool there
    ('git commit in a read-only data root', jail_world,
     lambda w: bash('git -C "%s" commit -m x' % f(w.ro), w.wt, True), None, 'write-jail'),
    ('git stash in a read-only data root', jail_world,
     lambda w: bash('git -C "%s" stash' % f(w.ro), w.wt, True), None, 'write-jail'),
    ('git stash -- show in another folder', jail_world,
     lambda w: bash('git -C "%s" stash -- show' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('git commit in the main checkout by -C, bash', jail_world,
     lambda w: bash('git -C "%s" commit -m x' % f(w.main), w.wt, True), None, 'git-main-history'),
    ('git commit in the main checkout by -C, PowerShell', jail_world,
     lambda w: ps('git -C "%s" commit -m x' % f(w.main), w.wt, True), None, 'git-main-history'),
    ('git commit in the main checkout by --git-dir', jail_world,
     lambda w: bash('git --git-dir="%s/.git" commit -m x' % f(w.main), w.wt, True), None, 'git-main-history'),
    ('git commit under GIT_CONFIG_PARAMETERS', jail_world,
     lambda w: bash("GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='/dev/null'\" git commit -m x", w.wt, True), None,
     'git-main-history'),
    # a subagent's reset --hard of a main checkout: reset --keep in its worktree
    ('git reset --hard of the main checkout by -C, bash', jail_world,
     lambda w: bash('git -C "%s" reset --hard' % f(w.main), w.wt, True), None, 'git-reset-hard'),
    ('git reset --hard of the main checkout by -C, PowerShell', jail_world,
     lambda w: ps('git -C "%s" reset --hard HEAD~1' % f(w.main), w.wt, True), None, 'git-reset-hard'),
    ('git reset --hard in the main checkout', jail_world,
     lambda w: bash('git reset --hard', w.main, True), None, 'git-reset-hard'),
    # KNOWN_FAILS
    ('gawk -d into another folder', jail_world,
     lambda w: bash('gawk -d"%s/x.txt" \'{print}\' a.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('ruby -C into another folder', jail_world,
     lambda w: bash('ruby -C "%s" -i -pe 1 x.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('cp -t into another folder', jail_world,
     lambda w: bash('cp -t"%s" a.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('git branch -D backlog on a branch store', branch_store_world,
     lambda w: bash('git branch -D backlog', w.wt, False), PM, 'git-branch-force-delete'),
    ('git push of the PM data branch', pmdata_world,
     lambda w: bash('git -C "%s" push --no-verify private pmdata' % f(w.main), w.wt, True), None,
     'git-push-pmdata'),
]


def filled(text, w):
    for k, v in FILL.items():
        text = text.replace(k, v)
    return text.replace('<worktree>', f(w.wt))


setUpModule = W.deny_softened           # as test_rules: the rows pin the softened rules as refusals
tearDownModule = W.allow_unplaced


class RowsPass(unittest.TestCase):
    """Each test_rules row's Instead, fed back."""

    def test_every_rows_instead_passes(self):
        checked = 0
        for name, make, kind, rule, instead in ROWS:
            w = W.World()
            try:
                w.git.ancestors.add((W.SHA_WT, 'main'))
                ev = make(w)
                d = decide(ev, w.ctx())
                if d is None or PLACEHOLDER.search(d.instead or ''):
                    continue
                with self.subTest(name):
                    why = again(ev, d.instead, w.ctx())
                    checked += 1
                    if name in KNOWN_FAILS:
                        self.assertIsNotNone(why, 'a known fail now passes: take it off KNOWN_FAILS: %s' % name)
                    else:
                        self.assertIsNone(why, '%s [%s]: %s' % (d.instead, d.rule, why))
            finally:
                w.cleanup()
        self.assertGreater(checked, 200)        # the rows were read, not skipped


class CasesPass(unittest.TestCase):
    """The table's Instead, its placeholders filled, fed back."""

    def test_every_cases_instead_passes(self):
        for name, world, make, environ, rule in CASES:
            w = world()
            try:
                with self.subTest(name):
                    ev = make(w)
                    ctx = w.ctx(environ=environ or {})
                    d = decide(ev, ctx)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
                    text = filled(d.instead, w)
                    self.assertIsNone(PLACEHOLDER.search(text), text)
                    why = again(ev, text, ctx)
                    if name in KNOWN_FAILS:
                        self.assertIsNotNone(why, 'a known fail now passes: take it off KNOWN_FAILS: %s' % name)
                    else:
                        self.assertIsNone(why, '%s: %s' % (text, why))
            finally:
                w.cleanup()

    def test_every_known_fail_is_a_row_or_a_case(self):
        names = {r[0] for r in ROWS} | {c[0] for c in CASES}
        self.assertEqual(sorted(set(KNOWN_FAILS) - names), [])


class Forms(unittest.TestCase):
    """The forms the fixes print, exactly."""

    def setUp(self):
        self.w = jail_world()
        self.addCleanup(self.w.cleanup)
        self.wt = f(self.w.wt)

    def instead(self, ev):
        d = decide(ev, self.w.ctx())
        self.assertIsNotNone(d, 'allowed')
        return d

    def test_a_read_only_or_add_only_data_root_gives_git_in_the_worktree(self):
        for root, verdict in ((self.w.ro, 'read-only for agent sessions'), (self.w.add, 'an add-only data folder')):
            for shell in (bash, ps):
                with self.subTest(root=root, shell=shell):
                    d = self.instead(shell('git -C "%s" merge-tree --write-tree a b' % f(root), self.w.wt, True))
                    self.assertEqual(d.instead, 'git -C "%s" merge-tree --write-tree a b' % self.wt)
                    self.assertIn(verdict, d.reason)
                    self.assertIn('The same command in %s:' % self.wt, d.reason)

    def test_a_commit_retargeted_to_the_worktree_is_the_kit_commit_tool(self):
        tool = rules.kit_tool(self.w.ctx(), 'commit')
        d = self.instead(bash('git -C "%s" commit -m x' % f(self.w.ro), self.w.wt, True))
        self.assertEqual(d.instead, 'python %s -C "%s" -m "<subject>" <each path you changed>' % (tool, self.wt))
        self.assertIn('plain git writes no Co-Authored-By trailer', d.reason)
        d = self.instead(bash('git -C "%s" stash' % f(self.w.ro), self.w.wt, True))
        self.assertEqual(d.instead, 'python %s -C "%s" -m "WIP: <what>" <each path you changed>' % (tool, self.wt))
        self.assertIn('the stash stack is shared', d.reason)

    def test_a_subagents_reset_hard_of_a_main_checkout_is_reset_keep_in_its_worktree(self):
        d = self.instead(bash('git -C "%s" reset --hard HEAD~1' % f(self.w.main), self.w.wt, True))
        self.assertEqual(d.instead, 'git -C "%s" reset --keep HEAD~1' % self.wt)
        d = self.instead(bash('git reset --hard', self.w.main, True))
        self.assertEqual(d.instead, 'git -C "<worktree>" reset --keep HEAD')
        # a main session that may edit the main checkout keeps reset --keep there
        d = self.instead(bash('git reset --hard', self.w.main, False))
        self.assertEqual(d.instead, 'git -C "%s" reset --keep HEAD' % f(self.w.main))


class DecisionPins(unittest.TestCase):
    """The refusal of every input whose Instead this branch changed: the same (verdict, rule) at
    the base and here (only the Instead text changed). The own-pull rows:
    test_backlog_role.OwnPullDecision; test_rules.RuleTable pins the rows' own."""

    def test_the_cases_refuse_as_before(self):
        for name, world, make, environ, rule in CASES:
            w = world()
            try:
                with self.subTest(name):
                    d = decide(make(w), w.ctx(environ=environ or {}))
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
            finally:
                w.cleanup()

    def test_more_inputs_refuse_as_before(self):
        w = jail_world()
        self.addCleanup(w.cleanup)
        main = f(w.main)
        for text, cwd, sub, rule in (
                ('git reset --hard', w.main, False, 'git-reset-hard'),
                ('git -C "%s" commit --amend --no-edit' % main, w.wt, True, 'git-main-history'),
                ('git -C "%s" cherry-pick abc123' % main, w.wt, True, 'git-main-history'),
                ("git -C %s -c alias.x='commit -m y' x" % main, w.wt, True, 'git-alias'),
                ("git -c 'alias.x=!godot --headless' x", w.wt, True, 'git-alias'),
                ('git -C "%s" stash push' % f(w.plain), w.wt, True, 'write-jail'),
                ('git -C "%s" rm -q x' % f(w.ro), w.wt, True, 'write-jail'),
                ('git -C "%s" rm -q x' % f(w.add), w.wt, True, 'write-jail')):
            for shell in (bash, ps):
                with self.subTest(text, shell=shell):
                    d = decide(shell(text, cwd, sub), w.ctx())
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)


if __name__ == '__main__':
    unittest.main()
