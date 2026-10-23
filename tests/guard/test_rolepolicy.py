"""kitlib.guard.rolepolicy over neutral Actions, so every host gets the same answers (docs/guard.md
"Role policy"). The Codex-only parts are not here: apply_patch, the native-hook identity and
the role binding are its adapter's.

The World's main checkout turns the policy on (guard.role_policy) and names a mode for some roles;
a role with none is judged by the shared rules alone, exactly as before. The World's kit folder
(Ctx.kit) holds no role files unless a test writes some (RoleFiles)."""
import os
import re
import unittest
from unittest import mock

from kitlib import content as kcontent
from kitlib import kitroot
from kitlib.guard import context, decide, rolepolicy
from kitlib.host import Action

from . import claudehost as CH
from . import world as W

MODES = {'senior-worker': 'write', 'reviewer': 'read', 'test-runner': 'test', 'lander': 'lander',
         'pm': 'controller', 'coordinator': 'read'}
LOCK = 'python tools/machine_lock.py'


class Rec(dict):
    """A backlog record as kitlib.backlog.store's Snapshot gives it: get() and all()."""

    def all(self, key):
        v = self.get(key)
        return [] if v is None else v if isinstance(v, list) else [v]


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


def kit_tree(root):
    """Make `root` look like a kit checkout to kitlib.content.oncommit.is_kit (content/roles/ and
    tools/render_content.py), with the kit's record, ledger and commit tools beside it."""
    os.makedirs(os.path.join(root, 'content', 'roles'), exist_ok=True)
    os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
    for name in ('render_content.py', 'backlog.py', 'commit.py', 'ledger.py'):
        with open(os.path.join(root, 'tools', name), 'w', encoding='utf-8') as f:
            f.write('')


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
                d = self.denied(self.shell(text, 'reviewer', shell='bash'), 'unplaced')
                self.assertIn('Run it with "%s" as the working directory' % W.fwd(self.w.temp), d.reason)
                self.allowed(self.shell(text, 'reviewer', shell='bash', cwd=self.w.temp))

    def test_inline_code_that_only_reads_is_still_refused_in_the_checkout(self):
        """The guard proves no code reads only: isolated (-I) code that only reads is refused in the
        read-only checkout like any other, and runs from temp."""
        code = "import json,sys; print(json.load(open(sys.argv[1]))['a'])"
        for text in ('python3 -I -c "%s" x.json' % code, 'python3 -c "%s" x.json' % code):
            with self.subTest(text):
                self.denied(self.shell(text, 'reviewer', shell='bash'), 'unplaced')
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
        # the shared owner-tool rule refuses the owner-only writes first; the policy refuses them too
        for args in ('hold whole', 'hold slots 1', 'stop id --forc', 'stop id --force', 'reserve', 'top r-1',
                     'game on', '--repo x unbook b1 --force', 'hold'):
            with self.subTest(args):
                d = rolepolicy.check(self.shell('%s %s' % (LOCK, args), 'pm'), self.w.ctx())
                self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'), args)
        self.assertEqual(self.decide(self.shell(LOCK + ' hold whole', 'pm')).rule, 'owner-tool')
        self.assertEqual(self.decide(self.shell(LOCK + ' division --set x', 'pm')).rule, 'owner-tool')
        self.assertEqual(self.decide(self.shell(LOCK + ' unbook b1 --force', 'pm')).rule, 'owner-tool')
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

    def test_a_path_naming_the_lock_cli_given_to_another_tool_is_no_lock_call(self):
        """machine_lock.py as an argument of any program but python running it (backlog.py,
        commit.py, a reader, git, echo, a test of it) is a path or text, not a lock call: the policy
        reads only the file a command runs (rules.tool_run)."""
        kit_tools, pm = self.w.kit_dir.replace('\\', '/') + '/tools', self.w.data.replace('\\', '/')
        backlog = 'python %s/backlog.py -C %s --session kit ' % (kit_tools, pm)
        refused = (backlog + 'set EX-LOCK-SLOTS --scope kitlib/lock --scope tests/lock '
                             '--scope tools/machine_lock.py --scope docs/lock-api.md',
                   backlog + 'set EX-LOCK-RUN-ID --scope kitlib/lock '
                             '--scope tools/machine_lock.py --scope tests/lock/test_run.py',
                   backlog + 'add EX-LOCK-STATUS --title "status --json" '
                             '--scope tools/machine_lock.py --scope kitlib/lock/api.py')
        others = ('cat tools/machine_lock.py', 'git diff -- tools/machine_lock.py', 'rg hold tools/machine_lock.py',
                  'py -3 -X utf8 %s/commit.py -m "machine_lock.py: hold" tools/machine_lock.py' % kit_tools,
                  'git log --oneline -- tools/machine_lock.py', 'git -C x show HEAD:tools/machine_lock.py',
                  'grep -n hold tools/machine_lock.py | head -5',
                  'Get-Content tools/machine_lock.py | Select-String hold',
                  'gc tools/machine_lock.py', 'sls x tools/machine_lock.py', 'echo tools/machine_lock.py status',
                  'Select-String -Path tools/machine_lock.py -Pattern hold',
                  'grep -c lock <<< "python tools/machine_lock.py stop 42"',
                  "cat <<'EOF'\ntools/machine_lock.py status\nEOF", 'grep -n hold < tools/machine_lock.py',
                  'python tools/backlog.py set EX-LOCK --scope tools/machine_lock.py',
                  'python ./tools/commit.py -m x tools/machine_lock.py',
                  'python -m unittest tests.lock.test_machine_lock', 'python tests/lock/test_machine_lock.py',
                  'uv run python -m unittest tests.lock.test_machine_lock',
                  "Get-ChildItem env: | Where-Object { $_.Name -match 'MACHINE_LOCK|KIT|GATE' } | "
                  "Format-Table Name, Value")
        # the backlog writes are judged by the backlog's roles (tests/guard/test_backlog_role.py),
        # never as a lock call: a pm subagent is unlisted there, so backlog-role refuses them; a
        # builder may add, and may set a record's text on its own pull (the record read pulled by
        # the branch checked out where the tool runs: FakeGit's `main` for the PM data folder)
        own = {'id': 'EX-LOCK', 'state': 'pulled', 'pulled_by': 'main'}
        for shell in ('bash', 'powershell'):
            for role in ('pm', 'reviewer', 'lander', 'senior-worker'):
                for text in refused + others:
                    with self.subTest(shell=shell, role=role, text=text):
                        self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))
            for text in refused:
                with self.subTest(shell=shell, text=text):
                    self.denied(self.shell(text, 'pm', shell=shell), 'backlog-role')
                with self.subTest(shell=shell, text=text, role='senior-worker'), \
                        mock.patch('kitlib.guard.backlogrole.read_record', lambda root, rid: Rec(own, id=rid)):
                    self.allowed(self.shell(text, 'senior-worker', shell=shell))

    def test_every_real_lock_call_is_still_read(self):
        """The lock CLI as the program, or as the script python runs, through the wrappers the
        guard unwraps."""
        lock = 'tools/machine_lock.py'
        rows = (('bash', 'python %s/tools/machine_lock.py hold --force' % self.w.kit_dir.replace('\\', '/')),
                ('bash', 'python "C:/Program Files/kit/tools/machine_lock.py" hold --force'),
                ('bash', 'py -3 %s hold --force' % lock), ('bash', 'py -3.12 %s hold --force' % lock),
                ('bash', 'pythonw %s hold --force' % lock), ('bash', 'pyw %s hold --force' % lock),
                ('bash', 'python3 -X utf8 -W ignore %s hold --force' % lock),
                ('bash', 'python -- %s hold --force' % lock), ('bash', '%s hold --force' % lock),
                ('bash', 'echo x | python %s hold --force' % lock), ('bash', 'cd x; py -3 %s hold --force' % lock),
                ('bash', 'git status && python %s hold --force' % lock),
                ('bash', 'env A=1 timeout 60 python %s hold --force' % lock),
                ('bash', 'start "" /b python %s hold --force' % lock),
                ('bash', 'bash -c "python %s hold --force"' % lock), ('bash', 'cmd /c "py %s hold --force"' % lock),
                ('powershell', 'python %s hold --force' % lock), ('powershell', '& py -3 %s hold --force' % lock),
                ('powershell', 'Get-Date; python %s hold --force' % lock),
                ('powershell', 'Start-Process python -ArgumentList "%s hold --force"' % lock),
                ('powershell', 'Start-Process -FilePath py -ArgumentList "%s", "hold", "--force"' % lock),
                ('powershell', '& "C:/Program Files/Python312/python.exe" %s hold --force' % lock),
                ('powershell', 'cmd /c "python %s hold --force"' % lock),
                ('powershell', 'pwsh -Command "python %s hold --force"' % lock))
        for shell, text in rows:
            with self.subTest(shell=shell, text=text):
                self.denied(self.shell(text, 'pm', shell=shell), 'role-lock')
        for shell, text in (('bash', 'py -3 %s run --kind build -- x' % lock),
                            ('powershell', 'Start-Process python -ArgumentList "%s background -- x"' % lock)):
            with self.subTest(shell=shell, text=text):
                self.denied(self.shell(text, 'reviewer', shell=shell), 'role-lock')

    def test_a_read_or_a_lock_call_whose_output_stays_still_passes(self):
        """The handed-on rule leaves alone a reader or git whose output reaches the terminal, a
        lock path printed to the terminal, and the lock CLI run itself (its output is the lock's,
        not its path), through a launcher or not, captured or not."""
        lock, kit = 'tools/machine_lock.py', self.w.kit_dir.replace('\\', '/')
        rows = (('powershell', 'gc %s' % lock), ('powershell', 'sls x %s' % lock),
                ('powershell', 'Get-Variable c; gc %s' % lock), ('bash', 'git show HEAD:%s' % lock),
                ('bash', 'grep -n def %s | head -5' % lock), ('bash', 'echo %s status' % lock),
                ('bash', 'grep -c lock <<< "%s"' % lock),
                ('bash', 'winpty python %s status' % lock),
                ('powershell', 'Start-Process python -ArgumentList "%s status"' % lock),
                ('bash', 'env A=1 timeout 60 python %s status' % lock),
                ('bash', 'bash -c "python %s status"' % lock),
                ('powershell', '$s = python %s/tools/machine_lock.py status' % kit),
                ('bash', 'python %s status | head -5' % lock))
        for shell, text in rows:
            for role in ('reviewer', 'senior-worker', 'pm'):
                with self.subTest(shell=shell, role=role, text=text):
                    self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))

    def test_the_lock_cli_run_as_a_module_is_read(self):
        """python -m with a module whose last part is machine_lock runs the lock CLI: its words
        are judged by their verb, as a run of the script is."""
        lock = 'tools/machine_lock.py'
        from kitlib.guard import commands as cm, shell as sh
        for shell, text in (('bash', 'python -m tools.machine_lock stop 42'),
                            ('bash', 'cd tools; python -m machine_lock stop 42'),
                            ('bash', 'python -u -m tools.machine_lock hold create x'),
                            ('powershell', 'py -3 -m tools.machine_lock stop 42')):
            with self.subTest(shell=shell, text=text):
                parsed = cm.read(text, sh.PS if shell == 'powershell' else sh.BASH, self.w.wt)
                self.assertIsNotNone(rolepolicy.lock_verb(parsed, 'read'))
                d = rolepolicy.check(self.shell(text, 'reviewer', shell=shell), self.w.ctx())
                self.assertIsNotNone(d, text)
                self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'), d.reason)
        for text in ('python -m tools.machine_lock status', 'python -m unittest tests.lock.test_machine_lock',
                     'python %s status' % lock):
            with self.subTest(text):
                self.assertIsNone(rolepolicy.check(self.shell(text, 'reviewer', shell='bash'), self.w.ctx()))
        for text in ('python -m pytest tests/lock/test_machine_lock.py', 'python tests/lock/test_machine_lock.py',
                     'python -m unittest -v tests.lock.test_machine_lock'):
            with self.subTest(text):
                self.assertIsNone(rolepolicy.check(self.shell(text, 'senior-worker', shell='bash'), self.w.ctx()))

    def test_a_reader_whose_plain_words_name_a_variable_still_passes(self):
        """A reader's words are text it searches for or prints, and a heredoc or a `<` file it
        reads is no lock call."""
        lock = 'tools/machine_lock.py'
        rows = (('powershell', 'sls PSVariable %s' % lock), ('powershell', "sls 'variable:' %s" % lock),
                ('powershell', 'Select-String -Pattern PSDefault -Path %s' % lock),
                ('bash', "cat <<'EOF'\n%s status\nEOF" % lock), ('bash', 'grep -n hold < %s' % lock),
                ('bash', 'grep -c lock <<< "python %s stop 42"' % lock))
        for shell, text in rows:
            for role in ('reviewer', 'senior-worker', 'pm'):
                with self.subTest(shell=shell, role=role, text=text):
                    self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))

    def test_an_option_value_naming_the_lock_before_the_lock_cli_reads_its_own_verb(self):
        """The same option values before the lock CLI run read-only pass, as they run status."""
        lock = 'tools/machine_lock.py'
        for shell, text in (('bash', 'python -X machine_lock %s status' % lock),
                            ('powershell', 'python -X machine_lock -X status %s status' % lock),
                            ('bash', 'pythonw -W machine_lock -m tools.machine_lock status'),
                            ('powershell', '%s python -ArgumentList "-X machine_lock %s status"'
                             % ('Start-' + 'Process', lock))):
            with self.subTest(shell=shell, text=text):
                self.assertIsNone(rolepolicy.check(self.shell(text, 'reviewer', shell=shell), self.w.ctx()))

    def test_plain_git_and_the_launchers_still_pass(self):
        """What the round's refusals leave alone: git reading the lock path with plain options, a
        colon after the name (not a stream Windows opens), a launcher's python running the lock
        CLI read-only, and PowerShell's $_ in a filter after the lock CLI itself."""
        lock = 'tools/machine_lock.py'
        rows = (('bash', 'git log -- %s' % lock), ('bash', 'git log -p -- %s' % lock),
                ('bash', 'git -C x show HEAD:%s' % lock), ('bash', 'git --no-pager diff -- %s' % lock),
                ('bash', 'git commit --dry-run -m "machine_lock.py: fix"'),
                ('powershell', 'git commit --dry-run -m "machine_lock.py: fix"'),
                ('bash', 'winpty python %s status' % lock), ('bash', 'bash -c "python %s status"' % lock),
                ('bash', 'bash -c "cd tools && python machine_lock.py status"'),
                ('bash', "bash <<'EOF'\npython %s status\nEOF" % lock),
                ('bash', 'cmd /c "python %s status"' % lock),
                ('powershell', 'pwsh -Command "python %s status"' % lock),
                # The empty title goes in by %, so the spawn-site scan (tests/proc) sees no start.
                ('bash', 'start %s /b python %s status' % ('""', lock)),
                ('bash', 'winpty python -m tools.machine_lock status'),
                ('bash', 'uv run python %s status' % lock),
                ('powershell', 'python %s status | ? { $_ -match "x" }' % lock))
        for shell, text in rows:
            for role in ('reviewer', 'pm'):
                with self.subTest(shell=shell, role=role, text=text):
                    self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))
                    if role == 'pm':                # a read role may not commit (write-jail)
                        self.allowed(self.shell(text, role, shell=shell))


class FreeTextNamesTheLock(Base):
    """A free-text option value (a record's --summary or --why-not-folded, a commit message) that
    names the lock CLI, one of its owner verbs or a whole
    lock command line is text, not a lock call, from any folder (not only the kit's export): the
    policy reads only what a command runs (rules.tool_run). The defect was the pre-cut word scan,
    gone since 861d77da9; these rows hold it gone, and every real lock call is still refused."""

    def test_a_free_text_value_naming_the_lock_is_no_lock_call(self):
        tools, data = self.w.base.replace('\\', '/') + '/tools', self.w.data.replace('\\', '/')
        lock = 'python tools/machine_lock.py'
        rows = ('python %s/backlog.py -C %s --session kit add EX-LOCK-TOP --summary '
                '"%s top r-1 and %s hold whole refuse a pm" --scope kitlib/lock/api.py '
                '--found-on none --why-not-folded "machine_lock.py run --force is the owner\'s"'
                % (tools, data, lock, lock),
                'python %s/backlog.py -C %s --session kit add EX-LOCK-HOLD --summary "machine_lock.py hold whole" '
                '--why-not-folded "python -m tools.machine_lock top; run --force"' % (tools, data),
                'python %s/commit.py -m "lock: %s top r-1 is the owner\'s" tools/machine_lock.py' % (tools, lock),
                'git commit -m "lock: machine_lock.py run --force and hold whole stay the owner\'s"',
                'echo "%s stop 42 --force"' % lock)
        for shell in ('bash', 'powershell'):
            for role in ('reviewer', 'pm'):
                for text in rows:
                    with self.subTest(shell=shell, role=role, text=text):
                        self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))

    def test_a_real_lock_call_beside_the_free_text_is_still_refused(self):
        tools, data = self.w.base.replace('\\', '/') + '/tools', self.w.data.replace('\\', '/')
        lock = '%s/machine_lock.py' % tools
        add = 'python %s/backlog.py -C %s --session kit add EX-X --summary "machine_lock.py top"' % (tools, data)
        rows = (('bash', 'python %s top' % lock), ('powershell', 'python %s top' % lock),
                ('bash', 'python -m tools.machine_lock top'), ('powershell', 'py -3 -m tools.machine_lock top'),
                ('powershell', ('Start-' + "Process python -ArgumentList '%s top'") % lock),
                ('bash', '%s; python %s top' % (add, lock)), ('powershell', '%s; python %s top' % (add, lock)),
                ('bash', 'git commit -m "x" && python %s hold whole' % lock))
        for shell, text in rows:
            for role in ('reviewer', 'pm'):
                with self.subTest(shell=shell, role=role, text=text):
                    d = rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx())
                    self.assertIsNotNone(d, text)
                    self.assertEqual((d.verdict, d.rule), ('deny', 'role-lock'), d.reason)


SHA = 'b85318db49bd'


class KitExport(Base):
    """rolepolicy.kit_export, which rules.kit_wrapper asks: only a folder kitlib.pluginsource would
    have written is the kit's export (<plugin.export_dir>/<sha12>): its name a 12-hex sha, its
    marker and kit.json in it, directly under the configured export root, and no root the session
    may write holding it or lying inside it. The kit's tools run from there, naming the lock CLI in
    their words, pass (refusals.md row 116)."""

    def setUp(self):
        super().setUp()
        self.export = os.path.join(self.w.base, 'plugin')
        self.root = self.export_tree(self.export, SHA)
        self.w.user_config({'plugin': {'export_dir': self.export}})

    def export_tree(self, parent, name, marker=True, manifest=True):
        root = os.path.join(parent, name)
        os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
        for tool in ('backlog.py', 'commit.py', 'ledger.py'):
            with open(os.path.join(root, 'tools', tool), 'w', encoding='utf-8') as f:
                f.write('')
        if marker:
            with open(os.path.join(root, '.pm-kit-export'), 'w', encoding='utf-8') as f:
                f.write(name + '\n')
        if manifest:
            with open(os.path.join(root, 'kit.json'), 'w', encoding='utf-8') as f:
                f.write('{"version": "1.0.0"}\n')
        return root.replace('\\', '/')

    def passes(self, rows, roles=('pm', 'coordinator', 'senior-worker')):
        for shell in ('bash', 'powershell'):
            for text in rows:
                for role in roles:
                    with self.subTest(shell=shell, role=role, text=text):
                        self.assertIsNone(rolepolicy.check(self.shell(text, role, shell=shell), self.w.ctx()))

    def is_export(self, root):
        return rolepolicy.kit_export(self.w.ctx(), root, self.shell('x', 'reviewer'), self.w.wt)

    def test_a_record_naming_the_lock_cli_in_its_text_passes(self):
        """refusals.md row 116, verbatim but for the folders: the coordinator's backlog add whose
        --summary named the lock CLI (`machine_lock run`; `machine_lock.py run` too)."""
        data = self.w.data.replace('\\', '/')
        row = ('python "%s/tools/backlog.py" -C "%s" --session kit add EX-LOCK-RUN-BAT-RESOLVE --summary '
               '"%s on Windows: \'-- ./gradlew\' fails WinError 193 ... or the heavy guard remedy should name '
               'the absolute .bat ..." --scope kitlib/lock/run.py --found-on none --why-not-folded "..."')
        self.passes([row % (self.root, data, 'machine_lock run'), row % (self.root, data, 'machine_lock.py run'),
                     'python %s/tools/backlog.py -C %s --session kit add EX-X --summary '
                     '"tools/hub.py answer and machine_lock.py stop"' % (self.root, data),
                     'python %s/tools/ledger.py add --repo kit --prov owner --q "may machine_lock.py run stop 42?" '
                     '--a "yes: tools/hub.py answer" --decides "machine_lock run" --source chat' % self.root])

    def test_a_commit_of_the_lock_cli_passes(self):
        """commit.py from the export folder, from a worktree of the kit, and git commit, given the
        lock CLI's path and a message naming it."""
        kit_tree(self.w.wt)
        files = 'tools/machine_lock.py tests/lock/test_cli.py docs/lock-api.md'
        wt = self.w.wt.replace('\\', '/')
        self.passes(['python %s/tools/commit.py -C %s -m "lock: giveback" %s' % (self.root, wt, files),
                     'python tools/commit.py -m "lock: giveback" %s' % files,
                     'python tools/ledger.py add --repo kit --q "machine_lock.py stop 42"',
                     'git commit -m "lock: fix machine_lock.py"',
                     'git commit -m "lock: fix machine_lock.py" tools/machine_lock.py'])

    def test_the_default_export_root_is_under_localappdata(self):
        self.w.user_config({})
        self.w.environ['LOCALAPPDATA'] = os.path.join(self.w.base, 'local')
        root = self.export_tree(os.path.join(self.w.base, 'local', 'pm-kit', 'plugin'), SHA)
        self.assertTrue(self.is_export(root))
        self.passes(['python %s/tools/backlog.py set EX-LOCK --scope tools/machine_lock.py' % root])

    def test_a_folder_that_only_looks_like_an_export_is_not_one(self):
        self.assertTrue(self.is_export(self.root))
        for parent, name, kw in ((self.export, 'notasha00000', {}), (self.export, 'b85318db49bdX', {}),
                                 (self.export, '0123456789ab', {'marker': False}),
                                 (self.export, 'ba9876543210', {'manifest': False}),
                                 (os.path.join(self.w.base, 'other'), SHA, {}),
                                 (os.path.join(self.export, 'pins'), SHA, {}),
                                 (os.path.join(self.root, 'tools'), SHA, {})):
            with self.subTest(parent=parent, name=name):
                self.assertFalse(self.is_export(self.export_tree(parent, name, **kw)))

    def test_an_export_root_the_session_may_write_is_not_the_kits(self):
        """An export_dir under temp (a root the session writes) holds folders it could have made."""
        temp_export = os.path.join(self.w.temp, 'plugin')
        root = self.export_tree(temp_export, SHA)
        self.w.user_config({'plugin': {'export_dir': temp_export}})
        self.assertFalse(self.is_export(root))

    def test_no_export_root_the_guard_can_name_fails_closed(self):
        """No plugin config and no LOCALAPPDATA (kitlib.pluginsource.settings raises), or a config
        the guard cannot read: the export folder is not the kit's."""
        self.w.user_config({})
        self.w.environ.pop('LOCALAPPDATA', None)
        self.assertFalse(self.is_export(self.root))
        self.w.user_config({'plugin': {'export_dir': 'relative/plugin'}})
        self.assertFalse(self.is_export(self.root))


class LockMcp(Base):
    """The lock MCP's tools are held to the same verbs as the CLI (rolepolicy.MCP_VERBS)."""

    def mcp(self, name, role='reviewer', prefix=None):
        from kitlib.guard import rules
        tool = (prefix if prefix is not None else rules.mcp_prefix(self.w.ctx())) + name
        return Action('mcp', tool=tool, cwd=self.w.wt, agent='a1' if role else None, agent_role=role)

    def test_every_lock_tool_has_a_verb(self):
        # the served reads, and the retired owner writes a server from an older release still serves
        from kitlib.lockmcp import OWNER_ONLY, server
        self.assertEqual(set(rolepolicy.MCP_VERBS), set(server.TOOLS) | set(OWNER_ONLY))

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


class ComputedCheckoutReads(Base):
    """git with a computed -C may be a main checkout, so the write jail passes only a plain read
    (rules.git_jail_unknown, rules.git_reads strict). A computed word in a read that has no option
    writing a file still reads; in log, show and diff (--output writes one) it reads only when
    literal text before it keeps it from being an option (rules.strict_dyn_read). Writes stay
    refused, for every session the jail holds (docs/guard.md "Write jail")."""

    # (label, role, environ): two subagents and two marked main sessions
    SESSIONS = (('senior-worker', 'senior-worker', {}), ('reviewer', 'reviewer', {}),
                ('pm', None, {context.ROLE_ENV: 'pm'}), ('coordinator', None, {context.ROLE_ENV: 'coordinator'}))

    def each(self, rows, check):
        for shell, text in rows:
            for label, role, environ in self.SESSIONS:
                with self.subTest(text, shell=shell, session=label):
                    check(self.shell(text, role, shell=shell), environ=dict(environ))

    def test_a_read_with_computed_words_passes(self):
        self.each([('powershell', 'git -C $p log -1 --format="$w %h %cr %s"'),     # observed refused 15:15
                   ('powershell', 'git -C $p rev-parse $ref'),
                   ('powershell', 'git -C $p show --stat "HEAD~$n"'),
                   ('bash', 'git -C "$p" log -1 --format="$w %h"'),
                   ('bash', 'git -C "$p" log --pretty="tformat:%h $w" -3'),
                   ('bash', 'git -C "$p" show --stat "HEAD~$n"'),
                   ('bash', 'git -C "$p" diff "origin/$b" -- "./$f"'),
                   ('bash', 'git -C "$p" rev-parse "$ref"'),
                   ('bash', 'git -C "$p" rev-parse $ref'),
                   ('bash', 'git -C "$p" ls-files "$f"'),
                   ('bash', 'git -C "$p" cat-file -p "$sha"'),
                   ('bash', 'git -C "$p" merge-base HEAD "$b"'),
                   ('bash', 'git -C "$p" for-each-ref --format="%(refname)" "$pat"')],
                  lambda a, **kw: self.allowed(a, **kw))

    def test_a_plain_read_still_passes(self):
        self.each([('bash', 'git -C "$p" log --oneline -3'), ('bash', 'git -C "$p" branch --list'),
                   ('powershell', 'git -C $p log -1 --format="%h %cr %s"')],
                  lambda a, **kw: self.allowed(a, **kw))

    def test_a_computed_word_that_may_be_an_option_that_writes_is_refused(self):
        self.each([('bash', 'git -C "$p" log "$R"'),                       # $R may be --output=<file>
                   ('bash', 'git -C "$p" show $sha'),
                   ('bash', 'git -C "$p" diff "--outp$x"'),
                   ('bash', 'git -C "$p" log "--output=$f"'),
                   ('bash', 'git -C "$p" log --format=$w'),                # bash splits it: a second word
                   ('bash', 'git -C "$p" log HEAD~$n'),
                   ('bash', 'git -C "$p" log --format="$(cat f)"'),
                   ('bash', 'git -C "$p" log --format="$w" --output=x.txt'),
                   ('powershell', 'git -C $p log $f'),                      # an array is many arguments
                   ('powershell', 'git -C $p log @f'),                      # a splat is a computed word too
                   ('powershell', 'git -C $p log -1 --format="$w" --outp=x.txt'),
                   # status, describe and branch are not loosened: they may write (an index refresh, a ref)
                   ('bash', 'git -C "$p" status "$f"'),
                   ('bash', 'git -C "$p" describe "$x"'),
                   ('bash', 'git -C "$p" branch --list "$pat"')],
                  lambda a, **kw: self.denied(a, 'unplaced', **kw))

    def test_a_powershell_word_that_runs_on_after_its_closing_quote_is_refused(self):
        # PowerShell ends an argument that opens with a quote at its closing quote: what follows is
        # another argument, which may be --output=<file> (rules.dyn_head)
        self.each([('powershell', 'git -C $p log \'HEAD\'"$f"'), ('powershell', 'git -C $p log "HEAD~"$f'),
                   ('powershell', 'git -C $p show "x$n"\'y\''), ('powershell', 'git -C $p diff "origin/$b""$f"')],
                  lambda a, **kw: self.denied(a, 'unplaced', **kw))
        # a word that opens with bare text runs on through its quotes as one argument
        self.each([('powershell', 'git -C $p log HEAD~"$n"'), ('powershell', 'git -C $p show "HEAD~$n"')],
                  lambda a, **kw: self.allowed(a, **kw))

    def test_a_write_stays_refused(self):
        self.each([('bash', 'git -C "$p" tag "v$n"'),
                   ('bash', 'git -C "$p" branch -m main old'), ('powershell', 'git -C $p branch -m main "$b"'),
                   ('bash', 'git -C "$p" config user.name "$n"')],
                  lambda a, **kw: self.denied(a, 'unplaced', **kw))
        # a symbolic-ref write moves a ref every worktree shares: git-shared-ref holds every
        # session but the PM's (which the jail still refuses as unplaced)
        self.each([('bash', 'git -C "$p" symbolic-ref HEAD refs/heads/x')],
                  lambda a, **kw: self.denied(a, 'unplaced' if kw['environ'].get(context.ROLE_ENV) == 'pm'
                                              else 'git-shared-ref', **kw))


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
