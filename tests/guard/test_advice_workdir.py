"""Start-Process -WorkingDirectory as the directory git and python start in (commands.run_dir),
the wrapped-gate alternative run bare, the kit folder advice names (rules.advice_kit), and the
reason a python whose code the guard does not read is refused with (docs/guard.md "Rules",
"Parsing" and "Kit paths in advice")."""
import json
import os
import unittest

from kitlib.guard import commands as cm
from kitlib.guard import shell as sh

from . import world as W
from .claudehost import decide
from .world import bash, ps

EXPORT = 'a1b2c3d4e5f6'


def verdict(d):
    return ('allow', None) if d is None else (d.verdict, d.rule)


class RunDir(unittest.TestCase):
    """commands.run_dir: the folder a Start-Process -WorkingDirectory names, else the command's."""

    def inv(self, text, cwd='C:\\w'):
        parsed = cm.read(text, sh.PS, cwd)
        # a subexpression's command is read too (Get-Location in `(Get-Location)`): take the outer one
        return [cm.invocation(c) for c in parsed.commands if c.text == text][0]

    def test_the_workdir_is_where_the_program_runs(self):
        inv = self.inv("Start-Process git -ArgumentList 'status' -WorkingDirectory C:\\m")
        self.assertEqual(os.path.normcase(cm.run_dir(inv)), os.path.normcase('C:\\m'))
        inv = self.inv("Start-Process git -ArgumentList 'status' -WorkingDirectory sub")
        self.assertEqual(os.path.normcase(cm.run_dir(inv)), os.path.normcase('C:\\w\\sub'))

    def test_no_workdir_is_the_command_s_directory(self):
        self.assertEqual(cm.run_dir(self.inv("Start-Process git -ArgumentList 'status'")), 'C:\\w')
        self.assertEqual(cm.run_dir(self.inv('git status')), 'C:\\w')

    def test_a_computed_or_unplaceable_workdir_is_none(self):
        for wd in ('$d', '(Get-Location)', 'C:', 'Env:\\x'):
            with self.subTest(wd):
                inv = self.inv("Start-Process git -ArgumentList 'status' -WorkingDirectory %s" % wd)
                self.assertIsNone(cm.run_dir(inv))


class StartProcessWorkdir(unittest.TestCase):
    """git and python started by Start-Process -WorkingDirectory <main checkout> from a worktree
    are judged in the main checkout they run in, not in the worktree the command is typed in."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def judge(self, text, cwd):
        return decide(ps(text, cwd, sub=True), self.w.ctx())

    def test_git_writes_in_the_main_checkout_it_starts_in(self):
        main = self.w.main
        for text, rule in (
                ("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory %s" % main, 'git-main-history'),
                ("Start-Process git -ArgumentList 'checkout','main' -WorkingDirectory %s" % main, 'git-checkout'),
                # a workdir the guard cannot place: unplaced
                ("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory $d", 'unplaced'),
                ("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory Z:", 'unplaced')):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.wt)), ('deny', rule))

    def test_git_in_the_worktree_it_starts_in_passes(self):
        wt = self.w.wt
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'checkout','main' -WorkingDirectory %s" % wt, wt))
        # from the main checkout, a git started in a worktree acts on the worktree
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'add','a.py' -WorkingDirectory %s" % wt,
                                     self.w.main))
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'status' -WorkingDirectory %s" % self.w.main, wt))

    def test_python_writes_the_main_checkout_it_starts_in(self):
        main = self.w.main
        # a python whose writes the guard does not read is unplaced; cp's literal target is placed
        for text, rule in (("Start-Process python -ArgumentList '-c','print(1)' -WorkingDirectory %s" % main, 'unplaced'),
                           ("Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % main, 'unplaced'),
                           ("Start-Process python -ArgumentList 'x.py' -WorkingDirectory $d", 'unplaced'),
                           ("Start-Process cp -ArgumentList 'a','b' -WorkingDirectory %s" % main, 'main-checkout')):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.wt)), ('deny', rule))
        self.assertIsNone(self.judge("Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % self.w.wt,
                                     self.w.wt))

    def test_a_worktree_workdir_does_not_free_a_writer_in_the_main_checkout(self):
        # only git is placed from the workdir alone; any other program is judged from both folders
        wt = self.w.wt
        for text, rule in (("Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % wt, 'unplaced'),
                           ("Start-Process cp -ArgumentList 'a','b' -WorkingDirectory %s" % wt, 'main-checkout')):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.main)), ('deny', rule))
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'add','a.py' -WorkingDirectory %s" % wt,
                                     self.w.main))

    def test_the_alternative_aims_the_workdir_at_the_worktree(self):
        d = self.judge("Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % self.w.main, self.w.wt)
        self.assertEqual(d.instead, "Start-Process python -ArgumentList 'x.py' -WorkingDirectory \"%s\"" % W.fwd(self.w.wt))

    def test_a_redirect_is_placed_from_the_outer_location(self):
        # PowerShell resolves -RedirectStandardOutput against its own location, not the workdir
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'status' -RedirectStandardOutput o.txt "
                                     "-WorkingDirectory %s" % self.w.main, self.w.wt))
        d = self.judge("Start-Process git -ArgumentList 'status' -RedirectStandardOutput o.txt "
                       "-WorkingDirectory %s" % self.w.wt, self.w.main)
        self.assertEqual(verdict(d), ('deny', 'main-checkout'))


class WrappedGateBare(unittest.TestCase):
    """A wrapped gate's alternative is the gate by absolute path with -C, run
    bare in the background; no redirection, no cd chain."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_a_worktree_s_own_gate_keeps_its_folder(self):
        tools = os.path.join(self.w.wt, 'tools')
        os.makedirs(tools)
        with open(os.path.join(tools, 'gate.py'), 'w', encoding='utf-8') as f:
            f.write('')
        d = decide(bash('python tools/gate.py --full 2>&1 | tail -5', self.w.wt, sub=True), self.w.ctx())
        self.assertEqual(d.rule, 'wrapped-gate')
        self.assertEqual(d.instead, 'python "%s/tools/gate.py" -C "%s" --full' % (W.fwd(self.w.wt), W.fwd(self.w.wt)))
        self.assertIn('run_in_background', d.reason)
        self.assertNotIn('cd chain', d.reason)
        self.assertNotIn(' > ', d.instead)

    def test_a_cd_chain_is_dropped(self):
        d = decide(bash('cd %s && python tools/gate.py --full | tail' % W.fwd(self.w.wt), self.w.main, sub=True),
                   self.w.ctx())
        self.assertEqual(d.instead, 'python "%s/tools/gate.py" -C "%s" --full' % (W.fwd(self.w.kit_dir), W.fwd(self.w.wt)))
        self.assertNotIn('working directory', d.reason)

    def test_a_computed_cd_adds_no_directory(self):
        # after `cd $x` the directory the gate runs in is unknown: no -C, run it from the worktree root
        d = decide(bash('cd $x && python tools/gate.py --full | tail', self.w.wt, sub=True), self.w.ctx())
        self.assertEqual(d.rule, 'wrapped-gate')
        self.assertEqual(d.instead, 'python tools/gate.py --full')
        self.assertNotIn(' -C ', d.instead)
        self.assertIn('from the worktree root', d.reason)

    def test_a_lock_run_from_elsewhere_says_where_it_runs(self):
        d = decide(bash('cd %s && python tools/machine_lock.py run --kind build -- python x.py | tail'
                        % W.fwd(self.w.wt), self.w.main, sub=True), self.w.ctx())
        self.assertEqual(d.rule, 'wrapped-gate')
        self.assertEqual(d.instead, 'python "%s/tools/machine_lock.py" run --kind build -- python x.py'
                         % W.fwd(self.w.kit_dir))
        self.assertIn('with "%s" as the working directory' % W.fwd(self.w.wt), d.reason)


class AdviceKit(unittest.TestCase):
    """Advice names the export the machine's plugin marketplace names
    when that export was made from the kit checkout the guard runs from."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'},
                              'heavy': {'programs': [{'program': 'python*', 'kind': 'build', 'why': 'test'}]}})
        self.addCleanup(self.w.cleanup)
        os.makedirs(os.path.join(self.w.kit_dir, '.git'))
        self.plugin = os.path.join(self.w.base, 'plugin')
        self.export = os.path.join(self.plugin, EXPORT)
        os.makedirs(os.path.join(self.export, 'tools'))
        os.makedirs(os.path.join(self.plugin, '.claude-plugin'))
        self.write(os.path.join(self.export, '.pm-kit-export'), {'checkout': self.w.kit_dir})
        self.write(os.path.join(self.export, 'kit.json'), {'version': '1.0.0'})
        self.write(os.path.join(self.plugin, '.claude-plugin', 'marketplace.json'),
                   {'name': 'pm-kit', 'plugins': [{'name': 'pm-kit', 'source': './%s' % EXPORT}]})
        self.w.user_config({'plugin': {'export_dir': self.plugin}})

    def write(self, path, data):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def heavy_advice(self):
        return decide(bash('python x.py', self.w.wt, sub=True), self.w.ctx())

    def test_advice_names_the_current_export(self):
        d = self.heavy_advice()
        self.assertEqual(d.rule, 'heavy')
        self.assertIn('python "%s/tools/machine_lock.py" run ' % W.fwd(self.export), d.instead)
        d = decide(bash('python tools/gate.py --full | tail', self.w.wt, sub=True), self.w.ctx())
        self.assertTrue(d.instead.startswith('python "%s/tools/gate.py" -C ' % W.fwd(self.export)), d.instead)

    def test_the_advised_lock_run_is_not_refused_as_heavy(self):
        self.assertIsNone(decide(bash(self.heavy_advice().instead, self.w.wt, sub=True), self.w.ctx()))

    def test_an_export_of_another_checkout_is_not_named(self):
        self.write(os.path.join(self.export, '.pm-kit-export'), {'checkout': self.w.main})
        self.assertIn('python "%s/tools/machine_lock.py" run ' % W.fwd(self.w.kit_dir), self.heavy_advice().instead)

    def test_no_marketplace_names_the_kit_checkout(self):
        os.remove(os.path.join(self.plugin, '.claude-plugin', 'marketplace.json'))
        self.assertIn('python "%s/tools/machine_lock.py" run ' % W.fwd(self.w.kit_dir), self.heavy_advice().instead)


class NoSleepWait(unittest.TestCase):
    """A python -c that only sleeps is code like any other: no sleep-wait reason of its own."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_a_sleep_in_the_main_checkout_is_code_counted_as_written(self):
        for code in ('import time;time.sleep(110)', 'from time import sleep; sleep(5.5)'):
            with self.subTest(code):
                d = decide(ps('python -c "%s"' % code, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(verdict(d), ('deny', 'unplaced'))
                self.assertNotIn('sleep-wait', d.reason)
                self.assertIn('the guard does not read what the code python is given writes', d.reason)

    def test_a_sleep_in_a_worktree_is_unchanged(self):
        self.assertIsNone(decide(ps('python -c "import time;time.sleep(110)"', self.w.wt, sub=True), self.w.ctx()))


class UnreadPython(unittest.TestCase):
    """A python whose code the guard does not read (rules.py_unread), refused in a main checkout,
    gives that as its reason: it names no file it writes."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_an_unread_program_says_the_guard_does_not_read_it(self):
        why = '(counted as written: the guard does not read what the code python is given writes)'
        for shell, text in (
                (ps, 'python -c "import sys; print(sys.argv)" a b'),
                (bash, 'python -c "import sys; print(sys.argv)" a "" b'),
                (ps, 'python -c "import os;os.remove(\'x\')"'),
                (bash, 'echo x | python -')):
            with self.subTest(text):
                self.counted(shell, text, why)

    def counted(self, shell, text, why):
        d = decide(shell(text, self.w.main, sub=True), self.w.ctx())
        self.assertEqual(verdict(d), ('deny', 'unplaced'))
        self.assertTrue(d.reason.startswith('the directory `'), d.reason)
        self.assertIn(why, d.reason)
        self.assertNotIn('writes files in the directory it runs in', d.reason)

    def test_a_script_powershell_may_hand_other_words_says_so(self):
        # a script writes nothing by itself: what is unread is the words PowerShell hands it
        why = '(counted as written: PowerShell may hand python other words than the guard read)'
        for text in ("python tools/x.py --note ''", "python tools/x.py -m 'a \"b\" c'"):
            with self.subTest(text):
                self.counted(ps, text, why)

    def test_a_start_process_python_says_its_command_line_is_unread(self):
        why = '(counted as written: Start-Process hands python a command line the guard does not read)'
        for text in ("Start-Process python -ArgumentList '-c','print(1)'",
                     "Start-Process python -ArgumentList 'tools/x.py'"):
            with self.subTest(text):
                self.counted(ps, text, why)

    def test_a_named_write_keeps_its_reason(self):
        d = decide(bash('cp a b', self.w.main, sub=True), self.w.ctx())
        self.assertEqual(verdict(d), ('deny', 'main-checkout'))
        self.assertIn('`cp a b` writes ', d.reason)
        self.assertNotIn('counted as written', d.reason)

    def test_an_unread_program_in_a_worktree_still_passes(self):
        self.assertIsNone(decide(ps('python -c "import sys; print(sys.argv)" a b', self.w.wt, sub=True),
                                 self.w.ctx()))


class WorktreeAdvice(unittest.TestCase):
    """A write refused in a main checkout is offered in the session's worktree: its own path when
    the call runs there, else <worktree>; a new worktree only in a clause for a session with none,
    never as the Instead (docs/guard.md "Main checkout")."""

    ONLY = 'only a session with no worktree makes one, first (git -C "%s" worktree add'

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.main, self.wt, self.kit = W.fwd(self.w.main), W.fwd(self.w.wt), W.fwd(self.w.kit_dir)

    def refused(self, payload, rule):
        d = decide(payload, self.w.ctx())
        self.assertEqual(verdict(d), ('deny', rule), d and d.reason)
        self.assertNotIn('worktree add', d.instead)
        return d

    def test_an_unplaced_target_from_main_never_offers_a_new_worktree(self):
        d = self.refused(bash('cp a "$X"', self.w.main, sub=True), 'unplaced')
        self.assertEqual(d.instead, 'cp a "<worktree>/<file>"')
        self.assertIn(self.ONLY % self.main, d.reason)

    def test_an_unplaced_target_with_no_word_to_aim_shows_the_worktrees(self):
        d = self.refused(bash('tar -xf a.tar -C"$X"', self.w.main, sub=True), 'unplaced')
        self.assertEqual(d.instead, 'git -C "%s" worktree list' % self.main)
        self.assertIn(self.ONLY % self.main, d.reason)

    def test_a_main_target_with_no_word_to_aim_shows_the_worktrees(self):
        # cp -t<dir> has no word the guard rewrites: it offered a new worktree as the Instead before
        d = self.refused(bash('cp -t%s/d a' % self.main, self.w.main, sub=True), 'main-checkout')
        self.assertEqual(d.instead, 'git -C "%s" worktree list' % self.main)
        self.assertIn(self.ONLY % self.main, d.reason)
        self.assertIn('(d there)', d.reason)

    def test_an_unknown_target_never_offers_a_new_worktree(self):
        # a place git cannot tell offered a new worktree as the Instead before, from a worktree too
        unknown = W.fwd(self.w.unknown)
        for payload, instead, only in (
                (bash('cp -t%s/d a' % unknown, self.w.wt, sub=True), 'git -C "%s" worktree list' % self.main, False),
                (bash('cp -t%s/d a' % unknown, self.w.main, sub=True), 'git -C "%s" worktree list' % self.main, True),
                (bash('cp a %s/b' % unknown, self.w.main, sub=True), 'cp a "<worktree>/b"', True),
                (bash('cp a %s/b' % unknown, self.w.wt, sub=True), 'cp a "%s/b"' % self.wt, False)):
            with self.subTest(payload['tool_input']['command'], cwd=payload['cwd']):
                d = self.refused(payload, 'unplaced')
                self.assertEqual(d.instead, instead)
                if only:
                    self.assertIn(self.ONLY % self.main, d.reason)
                else:
                    self.assertNotIn('worktree add', d.reason)

    def test_a_main_target_from_main_is_aimed_at_the_worktree(self):
        for payload, instead in (
                (bash('cp a b', self.w.main, sub=True), 'cp a "<worktree>/b"'),
                (W.event('Write', {'file_path': os.path.join(self.w.main, 'd', 'b.md'), 'content': 'x'},
                         self.w.main, sub=True), 'Write(file_path="<worktree>/d/b.md")'),
                (ps("python %s/tools/commit.py -C '%s' -m 'a \"b\" c' x.py" % (self.kit, self.main), self.w.main,
                    sub=True), 'python %s/tools/commit.py -C "<worktree>" -m \'a "b" c\' x.py' % self.kit)):
            with self.subTest(instead):
                rule = 'unplaced' if instead.startswith('python') else 'main-checkout'
                d = self.refused(payload, rule)
                self.assertEqual(d.instead, instead)
                self.assertIn(self.ONLY % self.main, d.reason)
                self.assertNotIn('(. there)', d.reason)

    def test_main_checkout_advice_in_worktree_names_own_worktree_path(self):
        commit = "python %s/tools/commit.py -C%s -m 'a \"b\" c' x.py"
        for payload, rule, instead in (
                (bash('cp a %s/b' % self.main, self.w.wt, sub=True), 'main-checkout', 'cp a "%s/b"' % self.wt),
                (ps('"x" > $env:OUT', self.w.wt, sub=True), 'unplaced', '"x" > "%s/<file>"' % self.wt),
                (ps(commit % (self.kit, " '%s'" % self.main), self.w.wt, sub=True), 'unplaced',
                 commit % (self.kit, ' "%s"' % self.wt)),
                (ps(commit % (self.kit, "'%s'" % self.main), self.w.wt, sub=True), 'unplaced',
                 commit % (self.kit, ' "%s"' % self.wt))):
            with self.subTest(payload['tool_input']['command']):
                d = self.refused(payload, rule)
                self.assertEqual(d.instead, instead)
                self.assertNotIn('worktree add', d.reason)


setUpModule = W.deny_softened           # these pin what the guard cannot place and the dropped rules (world.deny_softened)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
