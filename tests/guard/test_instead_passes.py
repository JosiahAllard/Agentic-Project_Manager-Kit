"""Every Instead the guard prints passes the guard for the same role (docs/guard.md "Rules": an alternative is a command to run as written). Each refusal's Instead is
run again with the refused call's payload (its session, agent and folder; a file tool's call as a
Bash one) and context, and must be allowed. An Instead with a placeholder (`<worktree>`,
`<subject>`) is a form to fill, not a command: the test_rules rows skip it; the table below fills
the ones it names. A bare `cd <folder>` does nothing, so it counts as a failure.

KNOWN_FAILS names each form that is refused again today, with why; it is checked both ways, so a
new failing form fails the test and so does a known one that now passes (take it off the list).
It is strict: every name on it must be a row or case that was fed back, so a stale entry (a row
renamed, removed or skipped for a placeholder) fails too.
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

KNOWN_FAILS = {
    # CASES below
    'Start-Process cp -WorkingDirectory into main':
        'a Start-Process program\'s -ArgumentList is a word the guard does not read, so any such cp is '
        'unplaced; no rewrite of it passes',
    'git push --no-verify of the main checkout\'s branch, no branch named':
        'the push names no branch, so it pushes the branch the main checkout has out; from the worktree '
        'it would push the worktree\'s, so the Instead stays in the main checkout, which the write jail '
        'refuses a subagent',
    'git push --no-verify of HEAD from the main checkout':
        'HEAD is the main checkout\'s branch only there: the Instead stays in the main checkout, which '
        'the write jail refuses a subagent',
    'git alias whose shell value is a try block running a heavy program':
        'GAP (kitlib/guard/rules.py alias_heavy_run, ~line 4863): the value `try { godot --headless } catch '
        '{ }` is not one clean command, so the lock-wrapped run is not offered and the plain try block is '
        'printed, which the heavy rule refuses again; the bare `godot --headless` value and the same alias '
        'wrapped in a try around the whole git call both get the machine_lock line',
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


def main_world():
    w = W.World()
    w.git.ancestors.add((W.SHA_WT, 'main'))
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
    # an attached writer option's value (gawk -d"..", cp -t"..") and a folder option (ruby -C) aimed
    # at the worktree
    ('gawk -d into another folder', jail_world,
     lambda w: bash('gawk -d"%s/x.txt" \'{print}\' a.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('ruby -C into another folder', jail_world,
     lambda w: bash('ruby -C "%s" -i -pe 1 x.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('cp -t into another folder', jail_world,
     lambda w: bash('cp -t"%s" a.txt' % f(w.plain), w.wt, True), None, 'write-jail'),
    # the backlog store's branch: no delete form passes, so the Instead reads it
    ('git branch -D backlog on a branch store', branch_store_world,
     lambda w: bash('git branch -D backlog', w.wt, False), PM, 'git-branch-force-delete'),
    # a subagent's push from the main checkout: the same push from its worktree
    ('git push of the PM data branch', pmdata_world,
     lambda w: bash('git -C "%s" push --no-verify private pmdata' % f(w.main), w.wt, True), None,
     'git-push-pmdata'),
    ('git push --no-verify of the main checkout\'s branch, no branch named', pmdata_world,
     lambda w: bash('git -C "%s" push --no-verify private' % f(w.main), w.wt, True), None, 'git-push-pmdata'),
    ('git push --no-verify of HEAD from the main checkout', pmdata_world,
     lambda w: bash('git -C "%s" push --no-verify private HEAD' % f(w.main), w.wt, True), None,
     'git-push-pmdata'),
    # an inline alias that makes a commit: the kit commit tool, which git-plain-commit names
    ('git -C main inline alias that commits', jail_world,
     lambda w: bash("git -C \"%s\" -c alias.x='commit -m y' x" % f(w.main), w.wt, True), None, 'git-alias'),
    ('git inline alias that commits', jail_world,
     lambda w: bash("git -c alias.x='commit -m y' x", w.wt, True), None, 'git-alias'),
    # a ::$DATA stream name single-quoted, so neither shell expands $DATA
    ('Set-Content of a ::$DATA stream in main', jail_world,
     lambda w: ps("Set-Content '%s/x.txt::$DATA' y" % f(w.main), w.wt, True), None, 'main-checkout'),
    ('Set-Content of a ::$DATA stream in another folder', jail_world,
     lambda w: ps("Set-Content '%s/x.txt::$DATA' y" % f(w.plain), w.wt, True), None, 'write-jail'),
    # from the main checkout: the jail's own checkout is a main one this role may not edit
    ('tar -C into another folder from the main checkout', main_world,
     lambda w: bash('tar -xf a.tar -C "%s" -C sub' % f(w.plain), w.main, True), None, 'write-jail'),
    ('Start-Process cp -WorkingDirectory into main', jail_world,
     lambda w: ps("Start-Process cp -ArgumentList 'a.txt','b.txt' -WorkingDirectory '%s'" % f(w.main), w.wt, True),
     None, 'main-checkout'),
    # git archive's output file is a write target: --prefix's value is not it, -o is
    ('git archive --prefix <p> -o out of the jail', jail_world,
     lambda w: bash('git archive --prefix p -o "%s/out.tar" HEAD' % f(w.plain), w.wt, True), None, 'write-jail'),
    ('git archive --prefix <p>/ -o into a read-only data root', jail_world,
     lambda w: bash('git archive --prefix p/ -o "%s/out.tar" HEAD' % f(w.ro), w.wt, True), None, 'write-jail'),
    # an alias that runs a heavy program: the machine lock's run line, wherever the git call sits
    ('git alias running a heavy program, PowerShell', jail_world,
     lambda w: ps("git -c 'alias.x=!godot --headless' x", w.wt, True), None, 'git-alias'),
    ('git alias running a heavy program inside a try block', jail_world,
     lambda w: ps("try { git -c 'alias.x=!godot --headless' x } catch { }", w.wt, True), None, 'git-alias'),
    ('git alias whose shell value is a try block running a heavy program', jail_world,
     lambda w: ps("git -c 'alias.x=!try { godot --headless } catch { }' x", w.wt, True), None, 'git-alias'),
]


def filled(text, w):
    for k, v in FILL.items():
        text = text.replace(k, v)
    return text.replace('<worktree>', f(w.wt))


def unfed(known, table, fed):
    """The names on `known` that are rows of `table` but were not fed back (`fed`), sorted."""
    names = {r[0] for r in table}
    return sorted(n for n in known if n in names and n not in fed)


setUpModule = W.deny_softened           # as test_rules: the rows pin the softened rules as refusals
tearDownModule = W.allow_unplaced


class RowsPass(unittest.TestCase):
    """Each test_rules row's Instead, fed back."""

    def test_every_rows_instead_passes(self):
        checked, fed = 0, set()
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
                    fed.add(name)
                    if name in KNOWN_FAILS:
                        self.assertIsNotNone(why, 'a known fail now passes: take it off KNOWN_FAILS: %s' % name)
                    else:
                        self.assertIsNone(why, '%s [%s]: %s' % (d.instead, d.rule, why))
            finally:
                w.cleanup()
        self.assertGreater(checked, 200)        # the rows were read, not skipped
        # strict: a known fail named for a row is one this test fed back (not skipped, not gone)
        self.assertEqual(unfed(KNOWN_FAILS, ROWS, fed), [])

    def test_row_names_are_unique(self):
        self.assertEqual(len(ROWS), len({r[0] for r in ROWS}))       # one name, one row

    def test_a_known_fail_for_a_skipped_row_is_reported(self):
        # a row skipped for a placeholder (or no refusal) is never fed: its known fail is stale
        table = [('fed row',), ('skipped row',)]
        known = {'fed row': 'why', 'skipped row': 'why', 'a case': 'why'}
        self.assertEqual(unfed(known, table, {'fed row'}), ['skipped row'])
        self.assertEqual(unfed(known, table, {'fed row', 'skipped row'}), [])


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
        self.assertEqual(len(CASES), len({c[0] for c in CASES}))      # one name, one case


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

    def test_a_reset_hard_with_no_worktree_shown_says_who_makes_one(self):
        d = self.instead(bash('git reset --hard', self.w.main, True))
        self.assertIn('only a session with no worktree makes one, first (git -C "%s" worktree add'
                      % f(self.w.main), d.reason)
        d = self.instead(bash('git -C "%s" reset --hard HEAD~1' % f(self.w.main), self.w.wt, True))
        self.assertNotIn('only a session with no worktree', d.reason)

    def test_git_from_a_folder_that_is_no_checkout_offers_no_git_there(self):
        # the folder the session may write is a plain one: git -C there has no repository
        d = self.instead(bash('git -C "%s" merge-tree --write-tree a b' % f(self.w.ro), self.w.plain, True))
        self.assertEqual((d.rule, d.instead), ('write-jail', 'pwd'))
        self.assertIn('To see where this session runs:', d.reason)


class JailForms(unittest.TestCase):
    """What the write jail answers for the forms the follow-up review named."""

    def setUp(self):
        self.w = jail_world()
        self.addCleanup(self.w.cleanup)
        self.wt = f(self.w.wt)

    def test_an_indexed_variable_target_is_unplaced_with_a_form_to_fill(self):
        # the parenthesised index is no literal path, as a plain $x[1] is none: the guard cannot tell
        # where Set-Content writes, and the Instead is the same cmdlet on a literal path in the worktree
        for target in ('$x[(1)]', '$x[1]'):
            with self.subTest(target):
                d = decide(ps('Set-Content %s y' % target, self.w.wt, True), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'unplaced'))
                self.assertIn('a target that is not literal: %s' % target, d.reason)
                self.assertEqual(d.instead, 'Set-Content "%s/<file>" y' % self.wt)

    def test_git_archive_names_its_output_file_as_the_write_target(self):
        # --prefix takes the next word (p), so the -o after it is read; out of the jail the answer is the
        # same archive written into the worktree, and that Instead passes
        for prefix in ('p', 'p/'):
            with self.subTest(prefix):
                ev = bash('git archive --prefix %s -o "%s/out.tar" HEAD' % (prefix, f(self.w.plain)), self.w.wt, True)
                d = decide(ev, self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'))
                self.assertIn('makes %s/out.tar' % f(self.w.plain), d.reason)
                self.assertEqual(d.instead, 'git -C "%s" archive --prefix %s -o "%s/out.tar" HEAD'
                                 % (self.wt, prefix, self.wt))
        # the same archive into the worktree writes inside the jail
        self.assertIsNone(decide(bash('git archive --prefix p -o out.tar HEAD', self.w.wt, True), self.w.ctx()))

    def test_read_only_retargets_the_same_verb_and_add_only_asks_for_a_new_name(self):
        # READ_ONLY: the same command on the same file name in the worktree (jail_refusal's rewrite);
        # ADD_ONLY, a file that exists: the same verb on <new file name> in the folder (a form to fill);
        # ADD_ONLY, a new file: allowed
        add_old = os.path.join(self.w.add, 'old.txt')
        os.makedirs(self.w.add, exist_ok=True)
        with open(add_old, 'w', encoding='utf-8') as fh:
            fh.write('x')
        for cmd, ro, add in (('cp a.txt "%s"', 'cp a.txt "%s/x.txt"', 'cp a.txt "%s/<new file name>"'),
                             ('mv a.txt "%s"', 'mv a.txt "%s/x.txt"', 'mv a.txt "%s/<new file name>"'),
                             ('Set-Content "%s" y', 'Set-Content "%s/x.txt" y', 'Set-Content "%s/<new file name>" y')):
            shell = ps if cmd.startswith('Set-Content') else bash
            with self.subTest(cmd):
                d = decide(shell(cmd % (f(self.w.ro) + '/x.txt'), self.w.wt, True), self.w.ctx())
                self.assertEqual((d.rule, d.instead), ('write-jail', ro % self.wt))
                self.assertIn('read-only for agent sessions', d.reason)
                d = decide(shell(cmd % f(add_old), self.w.wt, True), self.w.ctx())
                self.assertEqual((d.rule, d.instead), ('write-jail', add % f(self.w.add)))
                self.assertIn('an add-only data folder', d.reason)
        self.assertIsNone(decide(bash('cp a.txt "%s/new.txt"' % f(self.w.add), self.w.wt, True), self.w.ctx()))


class PushForms(unittest.TestCase):
    """git-push-pmdata from the main checkout: the worktree only for a push that names its branch."""

    def test_only_a_named_branch_moves_to_the_worktree(self):
        w = pmdata_world()
        self.addCleanup(w.cleanup)
        main, wt = f(w.main), f(w.wt)
        for words, where in (('private pmdata', wt), ('private +pmdata:pmdata', wt), ('private', main),
                             ('private HEAD', main), ('private HEAD:main', main), ('private @', main),
                             ('private pmdata HEAD', main), ('--all private', main)):
            with self.subTest(words):
                d = decide(bash('git -C "%s" push --no-verify %s' % (main, words), w.wt, True), w.ctx())
                self.assertEqual((d.rule, d.instead), ('git-push-pmdata', 'git -C "%s" push %s' % (where, words)))


class ExecForms(unittest.TestCase):
    """find -execdir aimed at the worktree runs as -exec, its reason saying why."""

    def test_execdir_with_only_options_and_braces_becomes_exec(self):
        w = main_world()
        self.addCleanup(w.cleanup)
        d = decide(bash('find %s -name "*.tmp" -execdir rm -f {} +' % f(w.main), w.wt, True), w.ctx())
        self.assertEqual(d.instead, 'find "%s/." -name "*.tmp" -exec rm -f {} +' % f(w.wt))
        self.assertIn('with -exec in place of -execdir, find hands it the same files by their full paths', d.reason)
        # a relative name of its own: -execdir writes it beside each file, so it is kept
        d = decide(bash('find %s -name "*.tmp" -execdir cp {} out.txt \\;' % f(w.main), w.wt, True), w.ctx())
        self.assertNotIn('-exec ', d.instead or '')


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
