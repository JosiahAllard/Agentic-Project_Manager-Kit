"""The owner's delegation list (docs/delegations.md): where it is read from, its schema, and the
hard exclusions no entry can lift. Every test writes a throwaway home; none reads the machine's."""
import copy
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.home import PM_REPO_FENCE_ENV, pm_repo
from kitlib import delegations
from kitlib.config import migration, user
from kitlib.delegations import schema


def base_op(**kw):
    o = {
        'id': 'D-001',
        'what': 'Remove a switch file.',
        'repo': 'app',
        'command': 'Remove-Item -LiteralPath Q:/srv/app/switch.txt',
        'shell': 'PowerShell',
        'args': [],
        'preconditions': [{'kind': 'file-exists', 'path': 'Q:/srv/app/switch.txt'}],
        'reversible': True,
        'undo': 'write the file again',
        'trigger': ['owner-session', 'hub-answer'],
        'elevated': False,
    }
    o.update(kw)
    return o


# The draft list the record was written from, with generic names and paths.
DRAFT = {
    '_status': 'a note: never read',
    'schema': 1,
    'operations': [
        base_op(),
        base_op(id='D-002', what='Rebuild the dev clone after a named branch lands.',
                command='powershell -ExecutionPolicy Bypass -File Q:/srv/app/ops/rebuild.ps1',
                args=[{'name': 'branch', 'pattern': '[a-z0-9/-]{1,60}'}],
                preconditions=[
                    {'kind': 'branch-landed', 'repo_path': 'Q:/srv/app', 'branch': '{branch}'},
                    {'kind': 'file-exists', 'path': 'Q:/srv/app/ops/rebuild.ps1'}],
                undo='rebuild again from the previous main',
                trigger=['owner-session', 'hub-answer', 'standing:after-landing']),
        base_op(id='D-003', what='Start a coordinator the start checklist lists as not running.',
                repo='*',
                command='python <kit>/tools/launch.py start --session {session} --account {account}',
                args=[{'name': 'session', 'pattern': '^[a-z0-9-]{1,40}$'},
                      {'name': 'account', 'pattern': '^[0-9]$'}],
                preconditions=[{'kind': 'checklist-line', 'text': 'not running'}],
                undo='launch.py stop --name <name>',
                trigger=['standing:pm-start-checklist']),
    ],
    'never': ['elevation', 'settings and rules files'],
}


class Home(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.pm = pm_repo(self)
        self.dir = os.path.join(self.pm, user.DIR_NAME)
        self.path = os.path.join(self.dir, delegations.FILE_NAME)
        self.home_copy = os.path.join(self.home, user.DIR_NAME, delegations.FILE_NAME)

    def write_text(self, text, encoding='utf-8'):
        os.makedirs(self.dir, exist_ok=True)
        with open(self.path, 'w', encoding=encoding) as f:
            f.write(text)

    def write(self, body):
        self.write_text(json.dumps(body))

    def load(self):
        return delegations.load(self.home)

    def refused(self, body):
        self.write(body)
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        return str(cm.exception)

    def refused_op(self, **kw):
        return self.refused({'schema': 1, 'operations': [base_op(**kw)]})


class Location(Home):

    def test_the_file_is_in_the_pm_repositorys_machine_config_folder(self):
        self.write(DRAFT)
        self.assertEqual(self.load().path, self.path)
        self.assertEqual(os.path.basename(os.path.dirname(self.path)), '.pm-kit')
        # delegations_file names the home folder's copy, the move's source, never read by load
        self.assertEqual(delegations.delegations_file(self.home), self.home_copy)

    def test_the_home_folders_copy_is_never_read(self):
        os.makedirs(os.path.dirname(self.home_copy))
        with open(self.home_copy, 'w', encoding='utf-8') as f:
            json.dump(DRAFT, f)
        with self.assertRaises(delegations.DelegationMissing):
            self.load()

    def test_no_pointer_means_nothing_is_delegated(self):
        with mock.patch.dict(os.environ, {PM_REPO_FENCE_ENV: ''}):
            user.forget()
            self.addCleanup(user.forget)
            with self.assertRaises(delegations.DelegationMissing) as cm:
                self.load()
        self.assertIn('no PM repo pointer', str(cm.exception))

    def test_a_machine_config_that_cannot_be_read_refuses(self):
        # still in the home folder with the pointer set: UNMOVED, an error, never "nothing"
        self.write(DRAFT)
        os.makedirs(os.path.join(self.home, user.DIR_NAME))
        with open(os.path.join(self.home, user.DIR_NAME, user.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write('{}')
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertNotIsInstance(cm.exception, delegations.DelegationMissing)
        self.assertIn('the machine config cannot be read', str(cm.exception))

    def test_missing_means_nothing_is_delegated(self):
        with self.assertRaises(delegations.DelegationMissing) as cm:
            self.load()
        self.assertIn('nothing is delegated', str(cm.exception))
        self.assertIsInstance(cm.exception, delegations.DelegationError)

    def test_the_old_folder_is_never_read(self):
        old = os.path.join(self.home, migration.OLD_DIR_NAME)
        os.makedirs(old)
        with open(os.path.join(old, delegations.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump(DRAFT, f)
        with self.assertRaises(delegations.DelegationMissing):
            self.load()

    def test_an_environment_variable_does_not_move_it(self):
        other = tempfile.mkdtemp(prefix='kit-other-')
        self.addCleanup(shutil.rmtree, other, True)
        for name in ('PM_KIT_DELEGATIONS', 'PM_KIT_HOME'):
            old = os.environ.get(name)
            os.environ[name] = other
            self.addCleanup(lambda n=name, v=old: os.environ.pop(n) if v is None
                            else os.environ.__setitem__(n, v))
        self.write(DRAFT)
        self.assertEqual(self.load().path, self.path)

    def test_a_folder_in_its_place_refuses(self):
        os.makedirs(self.path)
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertNotIsInstance(cm.exception, delegations.DelegationMissing)
        self.assertIn('not a file', str(cm.exception))

    def test_a_junction_for_the_folder_refuses(self):
        real = os.path.join(self.home, 'elsewhere')
        os.makedirs(real)
        with open(os.path.join(real, delegations.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump(DRAFT, f)
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', self.dir, real],
                           capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isjunction(self.dir):
            self.skipTest('cannot make a junction here')
        self.addCleanup(lambda: os.path.isjunction(self.dir) and os.rmdir(self.dir))
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertIn('link or junction', str(cm.exception))

    def test_a_symlink_for_the_file_refuses(self):
        real = os.path.join(self.home, 'real.json')
        with open(real, 'w', encoding='utf-8') as f:
            json.dump(DRAFT, f)
        os.makedirs(self.dir)
        try:
            os.symlink(real, self.path)
        except (OSError, NotImplementedError):
            self.skipTest('cannot make a symlink here')
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertIn('link or junction', str(cm.exception))


class Malformed(Home):

    def test_not_json(self):
        self.write_text('{"schema": 1,')
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertIn('not valid JSON', str(cm.exception))

    def test_a_key_written_twice(self):
        self.write_text('{"schema": 1, "schema": 1, "operations": []}')
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertIn('written twice', str(cm.exception))

    def test_not_utf8(self):
        os.makedirs(self.dir)
        with open(self.path, 'wb') as f:
            f.write(b'{"schema": 1, "operations": [], "never": ["\xff"]}')
        with self.assertRaises(delegations.DelegationError) as cm:
            self.load()
        self.assertIn('cannot be read', str(cm.exception))

    def test_top_level_must_be_an_object(self):
        for body in ([], 'x', 1, None):
            with self.subTest(body=body):
                self.assertIn('must be an object', self.refused(body))

    def test_schema_is_one_exactly(self):
        for v in (2, 0, '1', True, 1.0):
            with self.subTest(v=v):
                self.refused({'schema': v, 'operations': []})
        self.assertIn('unsupported schema 2', self.refused({'schema': 2, 'operations': []}))

    def test_schema_and_operations_are_required(self):
        self.assertIn('needs "schema"', self.refused({'operations': []}))
        self.assertIn('needs "operations"', self.refused({'schema': 1}))

    def test_an_unknown_key_refuses_at_every_level(self):
        self.assertIn('unknown key(s) extra', self.refused({'schema': 1, 'operations': [], 'extra': 1}))
        self.assertIn('unknown key(s) extra', self.refused_op(extra=1))
        msg = self.refused_op(args=[{'name': 'x', 'pattern': '[a-z]+', 'extra': 1}],
                              command='Remove-Item {x}')
        self.assertIn('unknown key(s) extra', msg)
        msg = self.refused_op(preconditions=[{'kind': 'file-exists', 'path': 'a', 'extra': 1}])
        self.assertIn('unknown key(s) extra', msg)

    def test_one_bad_entry_refuses_the_whole_file_and_every_problem_is_named(self):
        body = copy.deepcopy(DRAFT)
        body['operations'][0]['repo'] = 'Bad Repo'
        body['operations'][2]['shell'] = 'cmd'
        msg = self.refused(body)
        self.assertIn('operations[0].repo', msg)
        self.assertIn('operations[2].shell', msg)


class Schema(Home):

    def test_null_is_refused_for_every_key(self):
        for key in base_op():
            with self.subTest(key=key):
                self.assertIn('%s must not be null' % key, self.refused_op(**{key: None}))
        self.assertIn('never must not be null', self.refused({'schema': 1, 'operations': [], 'never': None}))
        self.assertIn('schema must not be null', self.refused({'schema': None, 'operations': []}))

    def test_every_operation_key_but_undo_is_required(self):
        for key in base_op():
            if key == 'undo':
                continue
            o = base_op()
            del o[key]
            with self.subTest(key=key):
                self.assertIn('needs "%s"' % key, self.refused({'schema': 1, 'operations': [o]}))

    def test_elevated_must_be_a_bool(self):
        for v in ('false', 0, 'no'):
            with self.subTest(v=v):
                self.assertIn('elevated must be true or false', self.refused_op(elevated=v))

    def test_ids(self):
        for v in ('D-1', 'd-001', 'X-001', 'D-001a'):
            with self.subTest(v=v):
                self.assertIn('.id must be an id', self.refused_op(id=v))
        twice = self.refused({'schema': 1, 'operations': [base_op(), base_op()]})
        self.assertIn('id written twice', twice)

    def test_repo_and_shell(self):
        for v in ('App', 'a b', '-app', '', '**'):
            with self.subTest(v=v):
                self.assertIn('.repo', self.refused_op(repo=v))
        self.assertIn('.shell must be one of', self.refused_op(shell='cmd'))

    def test_triggers(self):
        self.assertIn('must not be empty', self.refused_op(trigger=[]))
        self.assertIn('must be one of', self.refused_op(trigger=['cron']))
        self.assertIn('twice', self.refused_op(trigger=['owner-session', 'owner-session']))

    def test_reversible_needs_undo_and_hub_answer_needs_reversible(self):
        o = base_op()
        del o['undo']
        self.assertIn('reversible needs "undo"', self.refused({'schema': 1, 'operations': [o]}))
        msg = self.refused_op(reversible=False)
        self.assertIn('hub-answer needs reversible true', msg)
        self.write({'schema': 1, 'operations': [base_op(reversible=False, trigger=['owner-session'])]})
        self.assertFalse(self.load().operations[0].reversible)

    def test_preconditions(self):
        self.assertIn('.kind must be one of', self.refused_op(preconditions=[{'kind': 'process-running'}]))
        self.assertIn('needs "path"', self.refused_op(preconditions=[{'kind': 'file-exists'}]))
        msg = self.refused_op(preconditions=[{'kind': 'branch-landed', 'repo_path': 'srv/app',
                                              'branch': 'main'}])
        self.assertIn('an absolute path', msg)
        self.assertIn('must be an object', self.refused_op(preconditions=['file-exists']))

    def test_args_are_declared_unique_and_used(self):
        self.assertIn('{who} is not a declared argument', self.refused_op(command='Remove-Item {who}'))
        msg = self.refused_op(command='Remove-Item {x}',
                              args=[{'name': 'x', 'pattern': '[a-z]+'}, {'name': 'x', 'pattern': '[a-z]+'}])
        self.assertIn('names x twice', msg)
        msg = self.refused_op(args=[{'name': 'x', 'pattern': '[a-z]+'}])
        self.assertIn('x is used by no {x} token', msg)
        self.assertIn('argument name', self.refused_op(command='Remove-Item {X}',
                                                       args=[{'name': 'X', 'pattern': '[a-z]+'}]))

    def test_an_arg_pattern_must_compile_and_not_be_broad(self):
        def with_pattern(p):
            return self.refused_op(command='Remove-Item {x}', args=[{'name': 'x', 'pattern': p}])
        self.assertIn('does not compile', with_pattern('[a-z'))
        for p in ('.*', '.+', '[^/]+', r'\S*', '[a-z ]+', '[a-z;]+', r'[a-z$]+', '(?s).+', ''):
            with self.subTest(p=p):
                self.assertTrue(re.search('too broad|non-empty regular', with_pattern(p)))

    def test_command_characters(self):
        for cmd in ('Remove-Item a; Remove-Item b', 'Remove-Item a | Out-Null', 'Remove-Item $HOME',
                    'Remove-Item "a"', "Remove-Item 'a'", 'Remove-Item a && b', 'Remove-Item a > b',
                    'Remove-Item (Get-Item a)', 'Remove-Item @a', 'Remove-Item a,b', 'Remove-Item a*',
                    'Remove-Item %TEMP%', 'Remove-Item a`b', 'Remove-Item a#b', 'Remove-Item {a',
                    'Remove-Item [a]', 'Remove-Item a?'):
            with self.subTest(cmd=cmd):
                self.assertIn('refused character', self.refused_op(command=cmd))
        self.assertIn('must be a non-empty string', self.refused_op(command='   '))

    def test_placeholders_other_than_kit_are_unfilled(self):
        self.assertIn('unfilled placeholder <DataDir>',
                      self.refused_op(command='Remove-Item <DataDir>/switch.txt'))
        self.assertIn('unfilled placeholder <x>', self.refused_op(command='python <kit>/<x>/a.py'))
        msg = self.refused_op(preconditions=[{'kind': 'file-exists', 'path': '<DataDir>/switch.txt'}])
        self.assertIn('unfilled placeholder <DataDir>', msg)
        msg = self.refused_op(preconditions=[{'kind': 'file-exists', 'path': '{nope}/switch.txt'}])
        self.assertIn('{nope} is not a declared argument', msg)


class Exclusions(Home):
    """The hard exclusions (E1 to E5): fixed in code, refused whatever the file says."""

    def hit(self, eid, command=None, **kw):
        if command is not None:
            kw['command'] = command
        msg = self.refused_op(**kw)
        self.assertIn('%s, never delegable' % eid, msg)
        return msg

    def test_e1_elevated_true_refuses(self):
        msg = self.hit('E1', elevated=True)
        self.assertIn('operations[0] (D-001).elevated', msg)

    def test_e1_elevation_tools(self):
        for cmd in ('runas /user:admin Q:/srv/a.exe', 'RunAs.exe /user:admin Q:/srv/a.exe',
                    'sudo Remove-Item a', 'gsudo Remove-Item a', 'Q:/tools/PsExec64.exe -s a',
                    'psexec -s a', 'schtasks /Create /TN a /TR b', 'schtasks.exe /Run /TN a',
                    'Register-ScheduledTask -TaskName a', 'New-ScheduledTaskPrincipal -UserId a',
                    'Set-ScheduledTask -TaskName a', 'Set-ExecutionPolicy Unrestricted',
                    'New-Thing pwsh -Verb RunAs', 'New-Thing pwsh -VERB x',
                    'New-Thing pwsh -Verb:RunAs',
                    'New-Thing -RunLevel Highest', 'New-Thing -RunL Highest',
                    'New-Thing a -Credential admin', 'New-Thing a -Cred admin', 'tool /RL HIGHEST'):
            with self.subTest(cmd=cmd):
                self.hit('E1', cmd)
        self.assertIn("'runas' is elevation (E1", self.hit('E1', 'runas /user:admin a'))

    def test_e2_settings_rules_and_kit_control_files(self):
        for cmd in ('Remove-Item Q:/home/.claude/settings.json', 'Remove-Item Q:/srv/settings.local.json',
                    'Remove-Item Q:/home/.claude.json', 'Remove-Item Q:/home/.claude',
                    'Remove-Item Q:/srv/app/.codex/config.toml', 'Remove-Item Q:/home/.pm-kit/config.json',
                    'Remove-Item Q:\\home\\%s\\x' % migration.OLD_DIR_NAME, 'Remove-Item Q:/srv/app/CLAUDE.md',
                    'Remove-Item Q:/home/.codex.json',
                    'Remove-Item Q:/srv/app/AGENTS.md', 'Remove-Item Q:/home/owner-delegations.json',
                    'Remove-Item Q:/srv/app/.kit.json', 'Remove-Item Q:/kit/content/roles/pm.md',
                    'Remove-Item Q:/kit/content/skills/x/SKILL.md', 'Remove-Item Q:/srv/app/.git/hooks/pre-push',
                    'Remove-Item Q:/srv/app/.git/config', 'git config core.hooksPath x',
                    'python <kit>/tools/machine_setup.py', 'python <kit>/tools/render_content.py --write'):
            with self.subTest(cmd=cmd):
                self.hit('E2', cmd)

    def test_e2_in_a_precondition(self):
        msg = self.hit('E2', preconditions=[{'kind': 'file-exists', 'path': 'Q:/home/.claude/settings.json'}])
        self.assertIn('preconditions[0]', msg)

    def test_e3_the_machine_lock_owner_controls(self):
        for cmd in ('python <kit>/tools/machine_lock.py hold whole', 'python <kit>/tools/machine_lock.py top',
                    'python <kit>/tools/lock_ui.py', 'machine-lock division --set 1',
                    'python <kit>/tools/lockmcp.py', 'python <kit>/tools/lock_server.py',
                    'call lock_set_division', 'call lock_hold_create'):
            with self.subTest(cmd=cmd):
                self.hit('E3', cmd)

    def test_e4_opaque_code(self):
        for cmd in ('iex Get-Date', 'Invoke-Expression Get-Date', 'Invoke-Command -ScriptBlock x',
                    '. Q:/srv/a.ps1', 'cmd /c dir', 'cmd.exe /c dir', 'wsl ls', 'wsl.exe ls',
                    'powershell Get-Date', 'powershell -Command Get-Date', 'pwsh -c Get-Date',
                    'pwsh.exe -EncodedCommand abc', 'powershell -ec abc', 'powershell -e abc',
                    'powershell -enc abc', 'powershell -NoProfile', 'powershell -NoProfile Get-Date -File a.ps1',
                    'powershell -co Get-Date -File a.ps1', 'powershell /Command Get-Date',
                    'python -c print', 'python -m pip install x', 'python.exe -Ic print', 'py -m pip',
                    'python3 -Bm pip', 'pythonw -c x', 'python -X utf8 -c x', 'python -W ignore -m pip',
                    'bash -c ls', 'sh -c ls', 'bash -xc ls', 'Q:/tools/bash.exe -c ls'):
            with self.subTest(cmd=cmd):
                self.hit('E4', cmd)

    def test_e5_launcher_options_the_owner_keeps(self):
        for cmd in ('python <kit>/tools/launch.py start --repo app --no-agent',
                    'python <kit>/tools/launch.py start --repo app --no-a',
                    'python <kit>/tools/launch.py start --repo app --prompt hi',
                    'python <kit>/tools/launch.py start --repo app --prompt=hi',
                    'python <kit>/tools/launch.py start --repo app --pro=hi',
                    'python <kit>/tools/launch.py deep --force', 'python <kit>/tools/launch.py deep --forc',
                    'python <kit>/tools/launch.py consultant --replace',
                    'python <kit>/tools/launch.py stop --name a --pid 1',
                    'python <kit>/tools/launch.py install-autostart',
                    'python <kit>/tools/launch.py homes --repo app'):
            with self.subTest(cmd=cmd):
                self.hit('E5', cmd)

    def test_an_argument_value_hits_the_exclusions(self):
        arg = delegations.Arg('x', re.compile('[A-Za-z0-9./_-]{1,60}'))
        for value, eid in (('.claude/settings.json', 'E2'), ('runas', 'E1'), ('machine_lock.py', 'E3'),
                           ('iex', 'E4'), ('homes', 'E5')):
            with self.subTest(value=value):
                self.assertIn('%s, never delegable' % eid, ' '.join(delegations.check_arg(arg, value)))


class CheckArg(unittest.TestCase):

    def setUp(self):
        self.arg = delegations.Arg('session', re.compile('^[a-z0-9-]{1,40}$'))

    def test_a_good_value(self):
        self.assertEqual(delegations.check_arg(self.arg, 'app-coordinator'), [])

    def test_the_owner_pattern_must_match(self):
        self.assertIn('does not match', ' '.join(delegations.check_arg(self.arg, 'App')))

    def test_the_hard_rules_hold_whatever_the_pattern(self):
        loose = delegations.Arg('x', re.compile(r'[\s\S]{1,300}'))
        for value, why in (('-x', 'starts with -'), ('a/../b', 'holds ..'), ('a b', 'outside'),
                           ('a;b', 'outside'), ('$x', 'outside'), ('', 'outside'), ('a' * 201, 'outside')):
            with self.subTest(value=value):
                self.assertIn(why, ' '.join(delegations.check_arg(loose, value)))
        self.assertIn('must be a string', ' '.join(delegations.check_arg(loose, 3)))


class HappyPath(Home):

    def test_the_draft_loads(self):
        self.write(DRAFT)
        got = self.load()
        self.assertEqual(got.path, self.path)
        self.assertEqual(got.schema, 1)
        self.assertEqual([o.id for o in got.operations], ['D-001', 'D-002', 'D-003'])
        d3 = got.operations[2]
        self.assertEqual(d3.argv, ('python', '<kit>/tools/launch.py', 'start', '--session', '{session}',
                                   '--account', '{account}'))
        self.assertEqual([a.name for a in d3.args], ['session', 'account'])
        self.assertEqual(delegations.check_arg(d3.args[1], '3'), [])
        self.assertEqual(d3.trigger, ('standing:pm-start-checklist',))
        self.assertEqual(got.operations[1].preconditions[0],
                         (('branch', '{branch}'), ('kind', 'branch-landed'), ('repo_path', 'Q:/srv/app')))

    def test_plain_commands_pass_the_exclusions(self):
        for cmd in ('powershell -NoProfile -ExecutionPolicy Bypass -File Q:/srv/a.ps1 -Name x',
                    'pwsh -File Q:/srv/a.ps1', 'python -u Q:/srv/a.py --check', 'python -X utf8 Q:/srv/a.py',
                    'py -3 Q:/srv/a.py', 'bash Q:/srv/a.sh', 'git -C Q:/srv/app pull --ff-only',
                    'python <kit>/tools/launch.py start --repo app --account 2'):
            with self.subTest(cmd=cmd):
                self.write({'schema': 1, 'operations': [base_op(command=cmd)]})
                self.assertEqual(self.load().operations[0].command, cmd)

    def test_no_operations_is_a_list_that_delegates_nothing(self):
        self.write({'schema': 1, 'operations': []})
        self.assertEqual(self.load().operations, ())

    def test_notes_are_allowed_at_every_level(self):
        body = copy.deepcopy(DRAFT)
        body['operations'][0]['_why'] = 'a note'
        body['operations'][0]['preconditions'][0]['comment'] = 'a note'
        self.write(body)
        self.assertEqual(len(self.load().operations), 3)

    def test_a_bom_is_read(self):
        self.write_text(json.dumps(DRAFT), encoding='utf-8-sig')
        self.assertEqual(len(self.load().operations), 3)

    def test_load_writes_nothing(self):
        self.write(DRAFT)
        os.chmod(self.path, stat.S_IREAD)
        self.addCleanup(os.chmod, self.path, stat.S_IWRITE | stat.S_IREAD)
        before = sorted(os.listdir(self.dir)), os.path.getmtime(self.path)
        self.load()
        self.assertEqual((sorted(os.listdir(self.dir)), os.path.getmtime(self.path)), before)


if __name__ == '__main__':
    unittest.main()
