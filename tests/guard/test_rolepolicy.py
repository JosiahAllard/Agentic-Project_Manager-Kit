"""kitlib.guard.rolepolicy over neutral Actions, so every host gets the same answers (docs/guard.md
"Role policy"). The Codex-only parts are not here: apply_patch, the native-hook identity and
the role binding are its adapter's.

The World's main checkout turns the policy on (guard.role_policy) and names a mode for some roles;
a role with none is judged by the shared rules alone, exactly as before. The World's kit folder
(Ctx.kit) holds no role files unless a test writes some (RoleFiles)."""
import os
import re
import unittest

from kitlib import content as kcontent
from kitlib import kitroot
from kitlib.guard import context, decide, rolepolicy
from kitlib.host import Action

from . import claudehost as CH
from . import world as W

MODES = {'senior-worker': 'write', 'reviewer': 'read', 'test-runner': 'test', 'lander': 'lander',
         'pm': 'controller', 'coordinator': 'read'}
LOCK = 'python tools/machine_lock.py'


def kit(modes=MODES, on=True):
    out = {'repo': {'name': 'demo', 'push_block': True},
           'roles': {name: {'mode': m, 'why': 'test'} for name, m in modes.items()}}
    if on is not None:
        out['guard'] = {'role_policy': on}
    return out


def role_file(name, mode=None):
    return ('---\nname: %s\ndescription: A test role.\ntier: light\nsandbox: workspace-write\n%s'
            'capabilities: all\n---\nbody\n' % (name, 'mode: %s\n' % mode if mode else '')).encode()


def write_roles(kit_dir, modes):
    folder = os.path.join(kit_dir, 'content', 'roles')
    os.makedirs(folder, exist_ok=True)
    for name, m in modes.items():
        with open(os.path.join(folder, name + '.md'), 'wb') as f:
            f.write(role_file(name, m))


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
    def test_read_test_and_lander_roles_do_not_edit_their_checkout(self):
        for role in ('reviewer', 'test-runner', 'lander'):
            with self.subTest(role):
                d = self.denied(self.edit(os.path.join(self.w.wt, 'worker.py'), role), 'role-edit')
                self.assertIn('the %s role (mode %s)' % (role, MODES[role]), d.reason)
                self.denied(self.edit('worker.py', role), 'role-edit')        # relative to the checkout

    def test_they_edit_temp_and_scratch(self):
        """A scratch script or notes file outside the checkout: the write jail's, as for any session."""
        for role in ('reviewer', 'test-runner', 'lander'):
            with self.subTest(role):
                self.allowed(self.edit(os.path.join(self.w.temp, 'scratch', 'check.py'), role))
                self.denied(self.edit(os.path.join(self.w.plain, 'x.py'), role), 'write-jail')

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
    def test_a_shell_write_to_one_is_read_only_for_read_test_and_lander(self):
        for rel in ('CLAUDE.md', '.claude/settings.json', '.claude/agents/x.md'):
            for role in ('reviewer', 'test-runner', 'lander'):
                with self.subTest(rel=rel, role=role):
                    self.denied(self.shell("Set-Content -LiteralPath %s -Value 'changed'" % rel, role), 'write-jail')
        self.allowed(self.shell("Set-Content -LiteralPath a.txt -Value 'changed'", 'lander'))

    def test_write_and_controller_roles_stay_under_the_owner_approval_rules(self):
        """No extra root: an owner-approved edit of CLAUDE.md or .claude/ by an agent stays possible
        (pre-push checks the approval)."""
        for role in ('senior-worker', 'pm'):
            for rel in ('CLAUDE.md', os.path.join('.claude', 'agent-profile.md')):
                with self.subTest(role=role, rel=rel):
                    a = self.edit(os.path.join(self.w.wt, rel), role)
                    self.allowed(a)
                    ctx = self.w.ctx()
                    self.assertIs(rolepolicy.context(a, ctx), ctx)

    def test_the_real_config_is_not_changed(self):
        ctx = self.w.ctx()
        before = ctx.config(self.w.wt).section('guard')
        decide(self.shell('Set-Content -LiteralPath CLAUDE.md -Value x', 'lander'), ctx)
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

    def test_a_scratch_script_and_a_log_in_temp_pass(self):
        t = W.fwd(self.w.temp)
        for role in ('reviewer', 'test-runner'):
            for text in ('python "%s/check.py"' % t, 'python tools/gate.py --full > "%s/gate.log" 2>&1' % t,
                         'python tools/gate.py --full', 'git diff main...feature'):
                with self.subTest(role=role, text=text):
                    self.allowed(self.shell(text, role, shell='bash'))
            with self.subTest(role=role, text='log in the checkout'):
                self.denied(self.shell('python tools/gate.py --full > gate.log', role, shell='bash'), 'write-jail')

    def test_inline_code_runs_from_temp_not_from_the_checkout(self):
        """python -c and a script on stdin may write where they run; the jail cannot read the code,
        so it judges the folder: the checkout is refused, and the alternative names temp."""
        for text in ('python3 -c "print(1)"', "python3 - <<'EOF'\nprint(1)\nEOF"):
            with self.subTest(text):
                d = self.denied(self.shell(text, 'reviewer', shell='bash'), 'write-jail')
                self.assertIn('Run it with "%s" as the working directory' % W.fwd(self.w.temp), d.reason)
                self.allowed(self.shell(text, 'reviewer', shell='bash', cwd=self.w.temp))


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

    def test_a_script_on_standard_input_is_finite(self):
        for text in ("python3 - <<'EOF'\nprint(1)\nEOF", 'echo "print(1)" | python3', 'python3 < s.py',
                     "bash -s <<'EOF'\necho hi\nEOF", "python3 <<< 'print(1)'",
                     'echo x | python3 -u -'):
            with self.subTest(text):
                self.assertIsNone(rolepolicy.check(self.shell(text, shell='bash'), self.w.ctx()), text)
                self.allowed(self.shell(text, shell='bash'))

    def test_an_interpreter_left_on_the_terminal_is_still_refused(self):
        for text in ('python3 -', 'bash -s', 'echo x | python3 -i', 'echo x | python3 -; python3',
                     'echo x | bash -c bash', 'echo x | cmd /k dir'):
            with self.subTest(text):
                self.denied(self.shell(text, shell='bash'), 'role-shell')

    def test_only_the_interpreters_own_options_count(self):
        """A word after the script, the -c command or the -m module is the program's, not the
        interpreter's: `-i`, `-s` and `-` there are arguments."""
        for text, shell in (('python -m pytest -s tests/', 'bash'), ('python -m pip install -i URL foo', 'bash'),
                            ('python tools/foo.py -i input.txt', 'bash'), ('python tools/x.py --input -', 'bash'),
                            ('cat x | python script.py -i', 'bash'), ('python -s script.py', 'bash'),
                            ('bash script.sh -i', 'bash'), ('bash -c "echo hi" -', 'bash'),
                            ('pwsh -NoProfile -File x.ps1 -n 5', 'powershell'),
                            ('pwsh -EncodedCommand ZQBjAGgAbwA=', 'powershell'), ('pwsh -ec ZQBj', 'powershell'),
                            ('powershell -e ZQBj', 'powershell'), ('pwsh -nop x.ps1 -i', 'powershell'),
                            ('python -c "print(1)" -i', 'bash'), ('python -X utf8 -W ignore x.py -i', 'bash'),
                            ('python -Wignore -Bc "print(1)"', 'bash'), ('py -3 x.py -', 'bash'),
                            ('python -m pytest -s', 'powershell'), ('bash -- script.sh -i', 'bash'),
                            ('bash -o pipefail script.sh', 'bash'), ('bash -lc "ls"', 'bash'),
                            ('cmd /q /c dir /k', 'powershell'), ('python -V', 'bash'), ('python --help', 'bash'),
                            ('bash --version', 'bash')):
            with self.subTest(text):
                self.assertIsNone(rolepolicy.check(self.shell(text, shell=shell), self.w.ctx()), text)

    def test_interactive_options_of_the_interpreter_itself_are_still_refused(self):
        for text, shell in (('python', 'bash'), ('python -i', 'bash'), ('python -i script.py', 'bash'),
                            ('python -s', 'bash'), ('python -u', 'bash'), ('python -v', 'bash'),
                            ('python -X utf8', 'bash'), ('bash -i', 'bash'), ('bash -i script.sh', 'bash'),
                            ('bash -s foo', 'bash'), ('bash --', 'bash'), ('echo x | bash -i', 'bash'),
                            ('pwsh -NoExit -File x.ps1', 'powershell'), ('pwsh -noe -c 1', 'powershell'),
                            ('pwsh -NoProfile', 'powershell'), ('pwsh -Interactive', 'powershell'),
                            ('pwsh -c -', 'powershell'), ('cmd /k dir', 'powershell'), ('cmd /q', 'powershell'),
                            ('bash -c bash', 'bash'), ('bash -c "python -i"', 'bash')):
            with self.subTest(text):
                self.denied(self.shell(text, shell=shell), 'role-shell')

    def test_the_instead_names_a_finite_form(self):
        d = self.denied(self.shell('python -i'), 'role-shell')
        self.assertIn('Instead: python -c "<command>"', d.reason)


class LockVerbs(Base):
    def test_owner_and_forced_lock_commands_are_refused(self):
        # the shared owner-lock rule refuses the owner-only writes first; the policy refuses them too
        for args in ('hold whole', 'hold slots 1', 'stop id --forc', 'stop id --force', 'reserve', 'top r-1',
                     'game on', '--repo x unbook b1 --force', 'hold'):
            with self.subTest(args):
                d = rolepolicy.check(self.shell('%s %s' % (LOCK, args), 'pm'), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'), args)
        self.assertEqual(self.decide(self.shell(LOCK + ' hold whole', 'pm')).rule, 'owner-lock')
        self.assertEqual(self.decide(self.shell(LOCK + ' division --set x', 'pm')).rule, 'owner-lock')
        self.assertEqual(self.decide(self.shell(LOCK + ' unbook b1 --force', 'pm')).rule, 'owner-lock')
        self.denied(self.shell(LOCK + ' stop id --forc', 'pm'), 'role-lock')

    def test_the_agent_rights_pass_for_write_controller_and_test(self):
        """docs/guard.md "Lock CLI owner writes" "Still open"."""
        for role in ('senior-worker', 'pm', 'test-runner'):
            for args in ('hold agent --for 1h', 'hold list', 'hold end h-1', 'hold edit h-1 --for 2h',
                         'division', 'game', 'game show', '--repo x status'):
                with self.subTest(role=role, args=args):
                    self.assertIsNone(rolepolicy.check(self.shell('%s %s' % (LOCK, args), role), self.w.ctx()))

    def test_a_writer_runs_locked_jobs(self):
        for args in ('status', 'status --days 3', 'run --repo x --kind engine-test -- python -m unittest --force',
                     'background --repo x --kind build -- x', 'stop r-1'):
            with self.subTest(args):
                self.assertIsNone(rolepolicy.check(self.shell('%s %s' % (LOCK, args)), self.w.ctx()))
        self.assertIsNone(rolepolicy.check(self.shell('%s run --kind build -- x' % LOCK, 'test-runner'), self.w.ctx()))

    def test_a_read_role_only_reads_the_lock_and_a_lander_also_runs(self):
        self.assertIsNone(rolepolicy.check(self.shell(LOCK + ' status', 'reviewer'), self.w.ctx()))
        d = self.denied(self.shell(LOCK + ' run --kind build -- x', 'reviewer'), 'role-lock')
        self.assertIn('(status; never --force)', d.reason)
        for args in ('status', 'status --days 3', 'run --kind build -- python tools/gate.py'):
            with self.subTest(args):
                self.assertIsNone(rolepolicy.check(self.shell('%s %s' % (LOCK, args), 'lander'), self.w.ctx()))
        for args in ('background --kind build -- x', 'hold agent', 'stop r-1', 'run --forc -- x'):
            with self.subTest(args):
                d = self.denied(self.shell('%s %s' % (LOCK, args), 'lander'), 'role-lock')
                self.assertIn('(status, run; never --force)', d.reason)

    def test_a_force_before_any_verb_is_named_without_a_leading_space(self):
        from kitlib.guard import commands as cm, shell as sh
        parsed = cm.read(LOCK + ' --force status', sh.BASH, self.w.wt)
        self.assertEqual(rolepolicy.lock_verb(parsed, 'read')[0], '(no verb) --force')
        d = self.denied(self.shell(LOCK + ' --force status', 'reviewer'), 'role-lock')
        self.assertIn('machine_lock.py (no verb) --force is not', d.reason)

    def test_only_the_verbs_the_lock_cli_has_are_named(self):
        """tools/machine_lock.py's own parser: a verb it does not have (stats, book, unbook) is
        nobody's right."""
        import importlib.util
        spec = importlib.util.spec_from_file_location('machine_lock_cli', os.path.join(
            kitroot.root(), 'tools', 'machine_lock.py'))
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        sub = next(a for a in cli.parser()._actions if a.dest == 'cmd')
        for m, table in rolepolicy.LOCK_VERBS.items():
            with self.subTest(m):
                self.assertLessEqual(set(table), set(sub.choices), m)
        for args in ('stats', 'book --kind build', 'unbook b1'):
            with self.subTest(args):
                self.denied(self.shell('%s %s' % (LOCK, args), 'senior-worker'), 'role-lock')


class LockMcp(Base):
    """The lock MCP's tools are held to the same verbs as the CLI (rolepolicy.MCP_VERBS)."""

    def mcp(self, name, role='reviewer', prefix=None):
        from kitlib.guard import rules
        tool = (prefix if prefix is not None else rules.mcp_prefix(self.w.ctx())) + name
        return Action('mcp', tool=tool, cwd=self.w.wt, agent='a1' if role else None, agent_role=role)

    def test_every_lock_tool_has_a_verb(self):
        from kitlib.lockmcp import server
        self.assertEqual(set(rolepolicy.MCP_VERBS), set(server.TOOLS))

    def test_the_read_tools_are_status_for_every_mode(self):
        for name in ('lock_snapshot', 'lock_wait_change', 'lock_stats', 'lock_run', 'lock_hold'):
            for role in ('reviewer', 'lander', 'test-runner', 'senior-worker', 'pm'):
                with self.subTest(name=name, role=role):
                    a = self.mcp(name, role)
                    self.assertEqual(rolepolicy.mode(a, self.w.ctx())[1], MODES[role])
                    self.assertIsNone(rolepolicy.check(a, self.w.ctx()))
                    self.allowed(a)

    def test_a_write_tool_is_held_to_the_modes_verbs(self):
        for name, role, refused in (('lock_end_run', 'reviewer', True), ('lock_end_run', 'lander', True),
                                    ('lock_end_run', 'senior-worker', False), ('lock_hold_end', 'test-runner', False),
                                    ('lock_hold_end', 'reviewer', True), ('lock_hold_edit', 'pm', False),
                                    ('lock_hold_create', 'pm', True), ('lock_set_game_mode', 'senior-worker', True),
                                    ('lock_set_games', 'pm', True), ('lock_set_division', 'test-runner', True)):
            with self.subTest(name=name, role=role):
                d = rolepolicy.check(self.mcp(name, role), self.w.ctx())
                if refused:
                    self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'))
                    self.assertIn(name, d.reason)
                else:
                    self.assertIsNone(d)

    def test_another_server_name_and_an_unknown_lock_tool(self):
        d = rolepolicy.check(self.mcp('lock_end_run', 'reviewer', prefix='mcp__other__'), self.w.ctx())
        self.assertEqual(d.rule, 'role-lock')
        d = rolepolicy.check(self.mcp('lock_new_thing', 'pm'), self.w.ctx())
        self.assertEqual(d.rule, 'role-lock')

    def test_other_mcp_tools_and_the_owner_read_nothing(self):
        def load(*a, **k):
            raise AssertionError('the policy read a .kit.json for a call it does not judge')
        for a in (self.mcp('search', prefix='mcp__github__'),
                  Action('mcp', tool='mcp__github__search', cwd=self.w.wt)):
            with self.subTest(a):
                self.assertEqual(rolepolicy.mode(a, self.w.ctx(load=load)), (None, None))


class Switch(unittest.TestCase):
    """guard.role_policy is the one switch: off (false or unset), no mode holds any role."""

    def test_off_holds_no_role_whatever_the_roles_table_or_a_role_file_says(self):
        for on in (False, None):
            with self.subTest(on=on):
                w = W.World(kit=kit(on=on))
                self.addCleanup(w.cleanup)
                write_roles(w.kit_dir, {'reviewer': 'read', 'worker': 'read'})
                ctx = w.ctx()
                for role in ('reviewer', 'worker', 'lander'):
                    a = Action('edit', tool='Write', path=os.path.join(w.wt, 'a.py'), cwd=w.wt, agent='a1',
                               agent_role=role)
                    self.assertEqual(rolepolicy.mode(a, ctx), (None, None))
                    self.assertEqual(rolepolicy.screen(a, ctx), (None, ctx))
                    self.assertEqual(decide(a, ctx).verdict, 'allow')

    def test_a_switch_that_is_not_a_boolean_does_not_load(self):
        w = W.World(kit=kit(on='yes'))
        self.addCleanup(w.cleanup)
        self.assertIsNone(w.ctx().config(w.wt))


class RoleFiles(Base):
    """With the switch on, a role the roles table gives no mode takes its role file's."""
    modes = {'reviewer': 'write'}
    files = {'reviewer': 'read', 'worker': 'write', 'test-runner': 'test', 'lander': 'lander',
             'senior-worker': 'read', 'coordinator': 'controller', 'plain-role': None}

    def setUp(self):
        super().setUp()
        write_roles(self.w.kit_dir, self.files)

    def test_a_role_file_mode_holds_its_role(self):
        self.denied(self.edit(os.path.join(self.w.wt, 'a.py'), 'test-runner'), 'role-edit')
        self.denied(self.shell('python3 -i', 'worker'), 'role-shell')
        self.allowed(self.edit(os.path.join(self.w.wt, 'a.py'), 'worker'))

    def test_a_roles_key_under_the_old_name_holds_the_renamed_role(self):
        """.kit.json `roles.opus-worker.mode` holds the senior-worker agent (kitlib.content.ALIASES),
        over its role file's mode."""
        w = W.World(kit=kit({'opus-worker': 'write'}))
        self.addCleanup(w.cleanup)
        write_roles(w.kit_dir, {'senior-worker': 'read'})
        for kind in ('senior-worker', 'opus-worker'):
            with self.subTest(kind):
                a = Action('edit', tool='Write', path=os.path.join(w.wt, 'a.py'), cwd=w.wt, agent='a1', agent_role=kind)
                self.assertEqual(rolepolicy.mode(a, w.ctx())[1], 'write')

    def test_the_roles_table_wins_over_the_role_file(self):
        a = self.edit(os.path.join(self.w.wt, 'a.py'), 'reviewer')
        self.assertEqual(rolepolicy.mode(a, self.w.ctx()), ('reviewer', 'write'))
        self.allowed(a)

    def test_a_role_file_with_no_mode_or_no_file_is_not_held(self):
        for role in ('plain-role', 'unknown-role'):
            with self.subTest(role):
                self.allowed(self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), role))

    def test_an_old_name_takes_its_new_names_mode(self):
        a = self.edit(os.path.join(self.w.wt, 'a.py'), 'opus-worker')
        self.assertEqual(rolepolicy.mode(a, self.w.ctx()), ('opus-worker', 'read'))
        self.denied(a, 'role-edit')

    def test_a_main_session_is_held_by_its_marks_role_file(self):
        a = self.edit(os.path.join(self.w.wt, 'a.py'), role=None)
        for mark, want in (('coordinator', ('coordinator', 'controller')),
                           ('pm-kit:test-runner', ('test-runner', 'test')), ('plain', ('main', None))):
            with self.subTest(mark):
                self.assertEqual(rolepolicy.mode(a, self.w.ctx(environ={context.ROLE_ENV: mark})), want)

    def test_claudes_subagent_type_is_the_role_file_name(self):
        """Claude Code sends a kit subagent's type plugin-prefixed; the adapter's bare name is the
        role file's stem, so the policy finds its mode. Another plugin's prefix names no role."""
        ctx = self.w.ctx()
        path = os.path.join(self.w.wt, 'a.py')
        for agent_type, want in (('pm-kit:test-runner', ('test-runner', 'test')),
                                 ('test-runner', ('test-runner', 'test')),
                                 ('other:test-runner', ('subagent', None))):
            with self.subTest(agent_type):
                ev = W.event('Write', {'file_path': path, 'content': 'x'}, self.w.wt, sub=True,
                             agent_type=agent_type)
                self.assertEqual(rolepolicy.mode(CH.action(ev), ctx), want)


class Writes(Base):
    """A role file's `writes`: files a read role may still edit in its checkout, that role alone."""
    modes = {}

    def setUp(self):
        super().setUp()
        write_roles(self.w.kit_dir, {'reviewer': 'read', 'test-runner': 'test'})
        with open(os.path.join(self.w.kit_dir, 'content', 'roles', 'consultant.md'), 'wb') as f:
            f.write(role_file('consultant', 'read').replace(
                b'mode: read\n', b'mode: read\nwrites: [docs/ops/notes.local.md, scratch/*.txt]\n'))

    def path(self, rel):
        return os.path.join(self.w.main, *rel.split('/'))

    def consultant(self, action):
        return self.decide(action, environ={context.ROLE_ENV: 'consultant'})

    def test_the_named_files_take_a_file_tool_edit(self):
        for rel in ('docs/ops/notes.local.md', 'scratch/a.txt'):
            with self.subTest(rel):
                a = self.edit(self.path(rel), role=None, cwd=self.w.main)
                self.assertEqual(self.consultant(a).verdict, 'allow')

    def test_anything_else_in_the_checkout_is_refused(self):
        for rel in ('docs/ops/other.md', 'scratch/a.py', 'scratch/sub/a.txt', 'CLAUDE.md'):
            with self.subTest(rel):
                a = self.edit(self.path(rel), role=None, cwd=self.w.main)
                self.assertEqual(self.consultant(a).rule, 'role-edit')

    def test_only_the_role_that_names_them(self):
        a = self.edit(self.path('docs/ops/notes.local.md'), role=None, cwd=self.w.main)
        for mark in ('reviewer', 'test-runner'):
            with self.subTest(mark):
                self.assertEqual(self.decide(a, environ={context.ROLE_ENV: mark}).rule, 'role-edit')

    def test_a_shell_write_to_one_stays_refused(self):
        a = Action('shell', tool='Shell', command='echo x >> docs/ops/notes.local.md', cwd=self.w.main, shell='bash')
        self.assertEqual(self.consultant(a).rule, 'write-jail')


class MissingContent(Base):
    """The guard runs from a kit with no role files, or broken ones: the policy is off for the
    roles it cannot read, and nothing raises."""
    modes = {}

    def test_no_content_folder_is_off_for_every_role(self):
        ctx = self.w.ctx()
        self.assertEqual(kcontent.role_modes(self.w.kit_dir), {})
        for role in ('reviewer', 'test-runner', 'lander', 'worker'):
            with self.subTest(role):
                a = self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), role)
                self.assertIsNone(rolepolicy.mode(a, ctx)[1])
                self.assertEqual(rolepolicy.screen(a, ctx), (None, ctx))
                self.allowed(a)

    def test_a_broken_role_file_is_left_out_and_the_rest_are_read(self):
        write_roles(self.w.kit_dir, {'reviewer': 'read', 'lander': 'lander'})
        folder = os.path.join(self.w.kit_dir, 'content', 'roles')
        for name, data in (('reviewer', b'no front matter\n'), ('tester', role_file('tester', 'telepathy')),
                           ('misnamed', role_file('other', 'read'))):
            with open(os.path.join(folder, name + '.md'), 'wb') as f:
                f.write(data)
        self.assertEqual(kcontent.role_modes(self.w.kit_dir), {'lander': 'lander'})
        self.allowed(self.edit(os.path.join(self.w.wt, 'a.py'), 'reviewer'))
        self.denied(self.edit(os.path.join(self.w.wt, 'a.py'), 'lander'), 'role-edit')

    def test_a_roles_path_that_is_a_file_is_no_role_files(self):
        os.makedirs(os.path.join(self.w.kit_dir, 'content'))
        with open(os.path.join(self.w.kit_dir, 'content', 'roles'), 'w') as f:
            f.write('x')
        self.assertEqual(kcontent.role_modes(self.w.kit_dir), {})
        self.allowed(self.edit(os.path.join(self.w.wt, 'CLAUDE.md'), 'reviewer'))


class KitRoleFiles(unittest.TestCase):
    def test_every_kit_role_file_names_a_mode_the_policy_knows(self):
        modes = kcontent.role_modes(kitroot.root())
        roles = os.listdir(os.path.join(kitroot.root(), 'content', 'roles'))
        self.assertEqual(sorted(modes), sorted(n[:-3] for n in roles if n.endswith('.md')))
        self.assertEqual(set(modes.values()) - set(rolepolicy.MODES), set())


class Rules(Base):
    """rolepolicy.RULES, the plain words tools/agent_view.py shows, against the answers the guard
    gives each mode: a row holds a mode exactly when the guard refuses that mode by it."""
    HOLDERS = {'senior-worker': 'write', 'reviewer': 'read', 'test-runner': 'test', 'lander': 'lander',
               'pm': 'controller'}

    @staticmethod
    def row(modes):
        """The text of the write-jail row over `modes`."""
        return next(t for r, ms, t in rolepolicy.RULES if r == 'write-jail' and ms is modes)

    def test_every_role_rule_the_guard_refuses_by_is_a_row(self):
        import inspect
        said = set(re.findall(r"_deny\('(role-[a-z]+)'", inspect.getsource(rolepolicy)))
        self.assertEqual(said, {r for r, _, _ in rolepolicy.RULES if r.startswith('role-')})
        self.assertEqual(set(self.HOLDERS.values()), set(rolepolicy.MODES))

    def test_each_row_holds_the_modes_the_guard_holds(self):
        for role, m in self.HOLDERS.items():
            texts = [t for _, t in rolepolicy.rules_for(m)]
            held = {r for r, _ in rolepolicy.rules_for(m)}
            with self.subTest(role=role, mode=m):
                edit = self.edit(os.path.join(self.w.wt, 'worker.py'), role)
                if 'role-edit' in held:
                    self.denied(edit, 'role-edit')
                else:
                    self.allowed(edit)
                self.denied(self.shell('Get-Location', role, workdir=self.w.plain), 'role-workdir')
                self.denied(self.shell('python -i', role), 'role-shell')
                self.denied(self.shell(LOCK + ' status --force', role), 'role-lock')
                write = self.shell('Set-Content -LiteralPath a.txt -Value changed', role)
                if self.row(rolepolicy.READ_ONLY_CHECKOUT) in texts:
                    self.denied(write, 'write-jail')
                else:
                    self.allowed(write)
                ctx = self.w.ctx()
                roots = [r['path'] for r in rolepolicy.context(edit, ctx).config(self.w.wt)
                         .section('guard').get('data_roots') or []]
                self.assertEqual(os.path.join(self.w.wt, 'CLAUDE.md') in roots,
                                 self.row(rolepolicy.OWNER_PATHS_READ_ONLY) in texts)


if __name__ == '__main__':
    unittest.main()
