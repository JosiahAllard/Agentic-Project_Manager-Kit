"""kitlib.guard.rolepolicy over neutral Actions, so every host gets the same answers (docs/guard.md
"Role policy"). Ported from the Codex branch's tests/codex/test_guard.py and test_kit_adapter.py
(lock verbs), with the Codex-only parts left out: apply_patch, the native-hook identity and
the role binding are its adapter's.

The World's main checkout names a mode for some roles; a role with none is judged by the shared
rules alone, exactly as before."""
import os
import unittest

from kitlib.guard import context, decide, rolepolicy
from kitlib.host import Action

from . import world as W

MODES = {'senior-worker': 'write', 'reviewer': 'read', 'test-runner': 'test', 'lander': 'lander',
         'pm': 'controller', 'coordinator': 'read'}
LOCK = 'python tools/machine_lock.py'


def kit(modes=MODES):
    return {'repo': {'name': 'demo', 'push_block': True},
            'roles': {name: {'mode': m, 'why': 'test'} for name, m in modes.items()}}


class Base(unittest.TestCase):
    modes = MODES

    def setUp(self):
        self.w = W.World(kit=kit(self.modes))
        self.addCleanup(self.w.cleanup)

    def edit(self, path, role='senior-worker', cwd=None):
        return Action('edit', tool='Write', path=path, cwd=cwd or self.w.wt, agent='a1' if role else None,
                      agent_role=role, retarget='Write(file_path="{path}")')

    def shell(self, command, role='senior-worker', shell='powershell', workdir=None, cwd=None):
        return Action('shell', tool='Shell', command=command, cwd=cwd or self.w.wt, shell=shell,
                      agent='a1' if role else None, agent_role=role, workdir=workdir)

    def decide(self, action, **kw):
        return decide(action, self.w.ctx(**kw))

    def denied(self, action, rule, **kw):
        d = self.decide(action, **kw)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        self.assertIn('Instead: ', d.reason)
        self.assertTrue(d.reason.endswith('[kit guard: rule=%s role=%s]' % (rule, 'subagent' if action.agent else 'main')))
        return d

    def allowed(self, action, **kw):
        d = self.decide(action, **kw)
        self.assertEqual(d.verdict, 'allow', d.reason)


class Off(Base):
    modes = {}

    def test_no_mode_is_the_shared_rules_alone(self):
        ctx = self.w.ctx()
        for a in (self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), 'reviewer'),
                  self.shell('python -i', 'reviewer'), self.shell(LOCK + ' hold whole', 'reviewer'),
                  self.shell('Get-Location', workdir=self.w.plain)):
            with self.subTest(a):
                self.assertEqual(rolepolicy.screen(a, ctx), (None, ctx))
        self.allowed(self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), 'reviewer'))

    def test_the_owners_own_session_reads_no_config(self):
        w = W.World(kit=kit({'main': 'read'}))
        self.addCleanup(w.cleanup)

        def load(*a, **k):
            raise AssertionError('the policy read a .kit.json for the owner')
        a = Action('edit', tool='Write', path=os.path.join(w.wt, 'a.py'), cwd=w.wt)
        self.assertEqual(rolepolicy.mode(a, w.ctx(environ={}, load=load)), (None, None))
        # a marked main session with no named mark is `main`, and `main` can be given a mode
        self.assertEqual(rolepolicy.mode(a, w.ctx(environ={context.ROLE_ENV: 'plain'})), ('main', 'read'))

    def test_only_edits_and_shell_calls_are_read(self):
        w = W.World(kit=kit())
        self.addCleanup(w.cleanup)
        ctx = w.ctx()
        for kind in ('mcp', 'other', 'session_start'):
            self.assertEqual(rolepolicy.mode(Action(kind, cwd=w.wt, agent='a', agent_role='reviewer'), ctx),
                             (None, None))


class Edits(Base):
    def test_read_test_and_lander_roles_do_not_edit(self):
        for role in ('reviewer', 'test-runner', 'lander'):
            with self.subTest(role):
                d = self.denied(self.edit(os.path.join(self.w.wt, 'worker.py'), role), 'role-edit')
                self.assertIn('the %s role (mode %s)' % (role, MODES[role]), d.reason)

    def test_a_write_role_edits_its_worktree(self):
        self.allowed(self.edit(os.path.join(self.w.wt, 'worker.py')))

    def test_a_controller_gains_no_main_checkout_edit(self):
        self.denied(self.edit(os.path.join(self.w.main, 'a.py'), 'pm'), 'main-checkout')

    def test_an_unnamed_role_is_not_held(self):
        self.allowed(self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), 'worker'))

    def test_a_marked_main_session_is_held_by_its_mark(self):
        a = self.edit(os.path.join(self.w.wt, 'worker.py'), role=None)
        self.denied(a, 'role-edit', environ={context.ROLE_ENV: 'coordinator'})
        self.allowed(a, environ={})                      # the owner's own: no role, no mode


class OwnerPaths(Base):
    def test_host_owner_paths_in_the_checkout_are_read_only(self):
        for rel in ('CLAUDE.md', os.path.join('.claude', 'settings.json'), os.path.join('.claude', 'agents', 'x.md')):
            with self.subTest(rel):
                self.denied(self.edit(os.path.join(self.w.wt, rel)), 'write-jail')

    def test_a_shell_write_to_one_is_read_only_too(self):
        self.denied(self.shell("Set-Content -LiteralPath CLAUDE.md -Value 'changed'"), 'write-jail')
        self.allowed(self.shell("Set-Content -LiteralPath a.txt -Value 'changed'"))

    def test_the_real_config_is_not_changed(self):
        ctx = self.w.ctx()
        before = ctx.config(self.w.wt).section('guard')
        decide(self.edit(os.path.join(self.w.wt, 'CLAUDE.md')), ctx)
        self.assertEqual(ctx.config(self.w.wt).section('guard'), before)

    def test_the_worktrees_folder_stays_open_in_a_main_checkout(self):
        ctx = self.w.ctx()
        main = rolepolicy.owner_roots(ctx, self.w.main)
        self.assertIn(os.path.join(self.w.main, 'CLAUDE.md'), main)
        self.assertNotIn(os.path.join(self.w.main, '.claude'), main)
        self.assertIn(os.path.join(self.w.wt, '.claude'), rolepolicy.owner_roots(ctx, self.w.wt))


class ReadOnlyCheckout(Base):
    def test_a_read_or_test_role_writes_nothing_in_its_checkout(self):
        for role in ('reviewer', 'test-runner'):
            with self.subTest(role):
                self.denied(self.shell('Set-Content -LiteralPath a.txt -Value changed', role), 'write-jail')

    def test_temp_stays_open(self):
        self.allowed(self.shell('Set-Content -LiteralPath "%s" -Value x' % W.fwd(os.path.join(self.w.temp, 'a.txt')),
                                'reviewer'))


class Workdir(Base):
    def test_a_workdir_outside_the_checkout_is_refused(self):
        d = self.denied(self.shell('Get-Location', workdir=self.w.data), 'role-workdir')
        self.assertIn('Instead: cd "%s"' % W.fwd(self.w.wt), d.reason)
        self.denied(self.shell('Get-Location', workdir=self.w.plain), 'role-workdir')

    def test_a_subfolder_is_allowed(self):
        sub = os.path.join(self.w.wt, 'sub')
        os.makedirs(sub)
        self.allowed(self.shell('Get-Location', workdir=sub))

    def test_another_drive_is_outside(self):
        self.assertFalse(rolepolicy.inside('Q:\\outside' if os.name == 'nt' else '/nonexistent-q/outside', self.w.wt))


class ShellGap(Base):
    def test_interactive_interpreters_are_refused(self):
        for text, shell in (('powershell.exe', 'powershell'), ('python.exe -i', 'powershell'),
                            ('python.exe -', 'powershell'), ('python3.exe -q', 'powershell'),
                            ('bash.exe -l', 'bash'), ('pwsh.exe -NoLogo', 'powershell'),
                            ('bash -c bash', 'bash'), ('cmd /c cmd', 'powershell'),
                            ("powershell -c 'bash -c bash'", 'powershell'), ('cmd /k dir', 'powershell'),
                            ('python', 'bash')):
            with self.subTest(text):
                self.denied(self.shell(text, shell=shell), 'role-shell')

    def test_noexit_and_its_prefixes_are_refused(self):
        for p in ('-noe', '-NOEX', '-noexit', '-noex:$true', '/NoExit'):
            with self.subTest(p):
                self.denied(self.shell('powershell %s -c 1' % p), 'role-shell')

    def test_finite_runs_pass(self):
        for text in ("powershell -NoProfile -c 'Get-Location'", 'python -c 1', 'python x.py', 'python -m unittest',
                     'bash -c "echo hi"', 'cmd /c dir', 'python --version', 'git status'):
            with self.subTest(text):
                self.assertIsNone(rolepolicy.check(self.shell(text), self.w.ctx()), text)

    def test_the_instead_names_a_finite_form(self):
        d = self.denied(self.shell('python -i'), 'role-shell')
        self.assertIn('Instead: python -c "<command>"', d.reason)


class LockVerbs(Base):
    def test_owner_and_forced_lock_commands_are_refused(self):
        # the shared owner-lock rule refuses the owner-only writes first; the policy refuses them too
        for args in ('division --set x', 'hold whole', 'stop id --forc', 'stop id --force', 'reserve',
                     'hold agent --for 1h', '--repo x unbook b1'):
            with self.subTest(args):
                d = rolepolicy.check(self.shell('%s %s' % (LOCK, args), 'pm'), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'), args)
        self.assertEqual(self.decide(self.shell(LOCK + ' hold whole', 'pm')).rule, 'owner-lock')
        for args in ('stop id --forc', 'hold agent --for 1h', '--repo x unbook b1'):
            with self.subTest(args):
                self.denied(self.shell('%s %s' % (LOCK, args), 'pm'), 'role-lock')

    def test_a_writer_runs_locked_jobs(self):
        for args in ('status', 'stats', 'run --repo x --kind engine-test -- python -m unittest --force',
                     'background --repo x --kind build -- x', 'stop r-1'):
            with self.subTest(args):
                self.assertIsNone(rolepolicy.check(self.shell('%s %s' % (LOCK, args)), self.w.ctx()))
        self.assertIsNone(rolepolicy.check(self.shell('%s run --kind build -- x' % LOCK, 'test-runner'), self.w.ctx()))

    def test_read_and_lander_roles_only_read_the_lock(self):
        for role in ('reviewer', 'lander'):
            with self.subTest(role):
                self.assertIsNone(rolepolicy.check(self.shell(LOCK + ' status', role), self.w.ctx()))
                d = self.denied(self.shell(LOCK + ' run --kind build -- x', role), 'role-lock')
                self.assertIn('(status, stats)', d.reason)


if __name__ == '__main__':
    unittest.main()
