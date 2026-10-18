"""Start-Process -WorkingDirectory as the directory git and python start in (commands.run_dir),
the wrapped-gate alternative run bare, the kit folder advice names (rules.advice_kit), and the
sleep-wait refusal's reason (docs/guard.md "Rules", "Parsing" and "Kit paths in advice")."""
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
                ("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory $d", 'git-main-history'),
                ("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory Z:", 'git-main-history')):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.wt)), ('deny', rule))

    def test_git_in_the_worktree_it_starts_in_passes(self):
        wt = self.w.wt
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'checkout','main' -WorkingDirectory %s" % wt, wt))
        # from the main checkout, a git started in a worktree acts on the worktree
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'commit','-m','x' -WorkingDirectory %s" % wt,
                                     self.w.main))
        self.assertIsNone(self.judge("Start-Process git -ArgumentList 'status' -WorkingDirectory %s" % self.w.main, wt))

    def test_python_writes_the_main_checkout_it_starts_in(self):
        main = self.w.main
        for text in ("Start-Process python -ArgumentList '-c','print(1)' -WorkingDirectory %s" % main,
                     "Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % main,
                     "Start-Process python -ArgumentList 'x.py' -WorkingDirectory $d",
                     "Start-Process cp -ArgumentList 'a','b' -WorkingDirectory %s" % main):
            with self.subTest(text):
                self.assertEqual(verdict(self.judge(text, self.w.wt)), ('deny', 'main-checkout'))
        self.assertIsNone(self.judge("Start-Process python -ArgumentList 'x.py' -WorkingDirectory %s" % self.w.wt,
                                     self.w.wt))

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


class SleepWait(unittest.TestCase):
    """A python -c that only sleeps, refused in a main checkout, is
    named as a sleep-wait, not as writing files."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_a_sleep_in_the_main_checkout_says_sleep_wait(self):
        for code in ('import time;time.sleep(110)', 'from time import sleep; sleep(5.5)'):
            with self.subTest(code):
                d = decide(ps('python -c "%s"' % code, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(verdict(d), ('deny', 'main-checkout'))
                self.assertIn('is a sleep-wait', d.reason)
                self.assertNotIn('writes files in the directory it runs in', d.reason)
                self.assertNotIn('worktree add', d.instead)

    def test_a_sleep_in_a_worktree_is_unchanged(self):
        self.assertIsNone(decide(ps('python -c "import time;time.sleep(110)"', self.w.wt, sub=True), self.w.ctx()))

    def test_other_code_keeps_the_writes_reason(self):
        for code in ('import time;time.sleep(1);open(\'x\',\'w\')', 'import os;os.remove(\'x\')'):
            with self.subTest(code):
                d = decide(ps('python -c "%s"' % code, self.w.main, sub=True), self.w.ctx())
                self.assertEqual(verdict(d), ('deny', 'main-checkout'))
                self.assertNotIn('sleep-wait', d.reason)


if __name__ == '__main__':
    unittest.main()
