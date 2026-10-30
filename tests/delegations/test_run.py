"""kitlib.delegations.run: the delegated-operation runner (docs/delegations.md "Running").

Every case builds its own ledger in a temp folder outside any git checkout (its write locks sit
beside the file), its own program folder and a stand-in hub and spawn. Nothing reaches the real
ledger, the real hub or a real program, except the branch-landed cases, which build a repository
and its bare origin (tests/gitfixture.py).
"""
import datetime
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.delegations import place
from kitlib.delegations import run as drun
from kitlib.delegations import schema
from kitlib.hubsite import approvals as hubapprovals
from kitlib.hubsite import client as hubclient
from kitlib.hubsite import push as hubpush
from kitlib.hubsite import serve as hubserve
from kitlib.ledger import write as lwrite
from tests import gitfixture

NOW = datetime.datetime(2026, 10, 20, 12, 0, tzinfo=datetime.timezone.utc)
WHEN = drun.LEDGER_WHEN


def stamp(delta):
    return (NOW - delta).strftime(WHEN)


def operation(**kw):
    """One checked Operation (schema.check) of an op object; kw replaces its keys."""
    op = {'id': 'D-001', 'what': 'say a name', 'repo': 'kit', 'shell': 'PowerShell',
          'command': 'tool {name}', 'args': [{'name': 'name', 'pattern': '[a-z]+'}],
          'preconditions': [], 'reversible': True, 'undo': 'nothing to undo',
          'trigger': ['owner-session', 'hub-answer'], 'elevated': False}
    op.update(kw)
    return schema.check({'schema': 1, 'operations': [op]}, 'test')[0]


class Spawn(object):
    """A stand-in subprocess.run: records each call; `code` is the exit code it gives."""

    def __init__(self, code=0, error=None, check=None):
        self.calls, self.code, self.error, self.check = [], code, error, check

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.check:
            self.check(argv, kw)
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.code)


class Hub(object):
    """A stand-in hub client: `listing` gives the questions of a status."""

    def __init__(self, questions=(), error=None):
        self.questions, self.error, self.roles = list(questions), error, []

    def listing(self, collection, status=None, repo=None, role=None):
        self.roles.append(role)
        if self.error:
            raise self.error
        return [q for q in self.questions if status in (None, 'all') or q.get('status') == status]


class Registry(object):
    """A stand-in drun.read_registry: `values` maps (hive, key, name) to (value, expandable)."""

    def __init__(self, values=None):
        self.values = dict(values or {})

    def __call__(self, hive, key, name):
        return self.values.get((hive, key, name))


class Fixture(unittest.TestCase):
    """A split ledger (ledger_dir: kit.md and pm.md) and a program folder holding tool.exe."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-delegated-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.folder = os.path.join(self.dir, 'ledger')
        self.ledger = os.path.join(self.folder, 'kit.md')
        self.bin = os.path.join(self.dir, 'bin')
        os.makedirs(self.bin)
        self.exe = os.path.join(self.bin, 'tool.exe')
        open(self.exe, 'wb').close()
        self.cfg = {'ledger_dir': self.folder, 'ledger_repos': ['kit', 'pm']}
        self.environ = {'SYSTEMROOT': 'C:\\Windows'}
        self.reg = Registry({('HKLM', drun.MACHINE_ENV, 'Path'): (self.bin, False)})
        self.spawn = Spawn()

    def add(self, decides='run D-001 name=alpha', prov='typed', age=datetime.timedelta(hours=1),
            repo='kit', source='the owner, in session'):
        return lwrite.add(os.path.join(self.folder, repo + '.md'), repo, prov, 'Run it?', 'yes',
                          decides, source, when=stamp(age), repos=self.cfg['ledger_repos'],
                          split=True).id

    def text(self):
        with open(self.ledger, encoding='utf-8') as fh:
            return fh.read()

    def run_op(self, op=None, pairs=(('name', 'alpha'),), trigger=None, **kw):
        kw.setdefault('now', NOW)
        kw.setdefault('spawn', self.spawn)
        kw.setdefault('environ', self.environ)
        kw.setdefault('registry', self.reg)
        kw.setdefault('checks', {})
        return drun.run(op or operation(), list(pairs), trigger, kw.pop('cfg', self.cfg), **kw)

    def refused(self, needle, **kw):
        with self.assertRaises(drun.RunRefused) as cm:
            self.run_op(**kw)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self.spawn.calls, [], 'a refused run started a program')
        return str(cm.exception)


class Arguments(Fixture):

    def setUp(self):
        Fixture.setUp(self)
        self.rid = self.add()

    def test_an_undeclared_argument_is_refused(self):
        self.refused('colour is not an argument of D-001',
                     pairs=(('name', 'alpha'), ('colour', 'red')), trigger=self.rid)

    def test_a_missing_argument_is_refused(self):
        self.refused('name is missing', pairs=(), trigger=self.rid)

    def test_an_argument_given_twice_is_refused(self):
        self.refused('name is given twice', pairs=(('name', 'alpha'), ('name', 'alpha')),
                     trigger=self.rid)

    def test_a_value_the_pattern_does_not_match_is_refused(self):
        self.refused("'Alpha1' does not match", pairs=(('name', 'Alpha1'),), trigger=self.rid)

    def test_a_filled_command_that_hits_an_exclusion_is_refused(self):
        # Each value passes check_arg alone; in its place after powershell it is a bare command
        # before -File, opaque code (E4). Built by hand: the loader never sees a value.
        op = schema.Operation('D-009', 'ps', 'kit', 'PowerShell', 'powershell {a} -File x.ps1',
                              ('powershell', '{a}', '-File', 'x.ps1'),
                              (schema.Arg('a', __import__('re').compile('[A-Za-z-]+')),), (),
                              True, 'none', ('owner-session',))
        self.assertEqual(schema.check_arg(op.args[0], 'Get-Process'), [])
        self.add(decides='run D-009 a=Get-Process')
        self.refused('hits a hard exclusion', op=op, pairs=(('a', 'Get-Process'),), trigger='H-kit-0002')

    def test_a_filled_precondition_that_hits_an_exclusion_is_refused(self):
        # 'aude/x' alone hits nothing; in its place the path is under .claude/ (E2).
        op = operation(command='tool', args=[{'name': 'name', 'pattern': '[a-z/]+'}],
                       preconditions=[{'kind': 'file-exists', 'path': 'C:/x/.cl{name}'}])
        self.assertEqual(schema.check_arg(op.args[0], 'aude/x'), [])
        self.add(decides='run D-001 name=aude/x')
        self.refused('preconditions[0]', op=op, pairs=(('name', 'aude/x'),), trigger='H-kit-0002')

    def test_the_run_line_sorts_the_arguments_by_name(self):
        op = operation(command='tool {b} {a}', args=[{'name': 'b', 'pattern': '[a-z]+'},
                                                     {'name': 'a', 'pattern': '[a-z]+'}])
        self.assertEqual(drun.run_line(op, {'b': 'y', 'a': 'x'}), 'run D-001 a=x b=y')


class Program(Fixture):

    def setUp(self):
        Fixture.setUp(self)
        self.rid = self.add()

    def test_the_child_gets_an_argument_list_and_no_shell(self):
        res = self.run_op(trigger=self.rid)
        self.assertEqual(len(self.spawn.calls), 1)
        argv, kw = self.spawn.calls[0]
        self.assertEqual(argv, [os.path.normpath(self.exe), 'alpha'])
        self.assertIsInstance(argv, list)
        self.assertIs(kw['shell'], False)
        self.assertEqual(kw['executable'], os.path.normpath(self.exe))
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)
        self.assertEqual((res.returncode, res.error), (0, None))

    def test_the_child_environment_is_the_allowlist_and_the_registry(self):
        decoy = os.path.join(self.dir, 'decoy')
        env = dict(self.environ, PYTHONPATH='C:/evil', PythonHome='C:/evil', PYTHONSTARTUP='x.py',
                   GIT_SSH_COMMAND='C:/evil/ssh.exe', GIT_DIR='C:/evil', KEEP='me',
                   PATH=decoy, PathExt='.EVIL', PSModulePath='C:/evil', temp='C:/t', ComSpec='C:/c')
        self.reg.values[('HKLM', drun.MACHINE_ENV, 'PATHEXT')] = ('.COM;.EXE', False)
        self.reg.values[('HKLM', drun.MACHINE_ENV, 'PSModulePath')] = ('C:/mods', False)
        self.run_op(trigger=self.rid, environ=env)
        child = self.spawn.calls[0][1]['env']
        self.assertEqual(child, {'TEMP': 'C:/t', 'PATH': self.bin, 'PATHEXT': '.COM;.EXE',
                                 'PSModulePath': 'C:/mods'})
        self.assertNotIn(decoy, ''.join(child.values()))

    def test_the_system_and_profile_folders_come_from_the_registry_not_the_caller(self):
        # git finds its global config through the profile names, cmd.exe is ComSpec, PowerShell
        # builds its module path from ProgramFiles: a caller's value would steer the child.
        names = ('SystemRoot', 'windir', 'SystemDrive', 'ComSpec', 'ProgramFiles',
                 'ProgramFiles(x86)', 'ProgramW6432', 'CommonProgramFiles',
                 'CommonProgramFiles(x86)', 'ProgramData', 'ALLUSERSPROFILE', 'PUBLIC',
                 'USERPROFILE', 'HOMEDRIVE', 'HOMEPATH', 'APPDATA', 'LOCALAPPDATA')
        env = dict({n: 'C:\\evil' for n in names}, USERNAME='someone')
        hklm, vol = 'HKLM', ('HKCU', drun.VOLATILE_ENV)
        self.reg.values.update({
            (hklm, drun.NT_CURRENT, 'SystemRoot'): ('C:\\Windows', False),
            (hklm, drun.MACHINE_ENV, 'ComSpec'): ('%SystemRoot%\\system32\\cmd.exe', True),
            (hklm, drun.WIN_CURRENT, 'ProgramFilesDir'): ('C:\\Program Files', False),
            (hklm, drun.WIN_CURRENT, 'ProgramFilesDir (x86)'): ('C:\\Program Files (x86)', False),
            (hklm, drun.WIN_CURRENT, 'ProgramW6432Dir'): ('C:\\Program Files', False),
            (hklm, drun.WIN_CURRENT, 'CommonFilesDir'): ('C:\\Program Files\\Common Files', False),
            (hklm, drun.WIN_CURRENT, 'CommonFilesDir (x86)'): ('C:\\x86\\Common Files', False),
            (hklm, drun.PROFILE_LIST, 'ProgramData'): ('%SystemDrive%\\ProgramData', True),
            (hklm, drun.PROFILE_LIST, 'Public'): ('%SystemDrive%\\pub', True),
            vol + ('USERPROFILE',): ('D:\\home\\owner', False),
            vol + ('HOMEDRIVE',): ('D:', False),
            vol + ('HOMEPATH',): ('\\home\\owner', False),
            vol + ('APPDATA',): ('D:\\home\\owner\\roaming', False),
            vol + ('LOCALAPPDATA',): ('D:\\home\\owner\\local', False),
        })
        self.run_op(trigger=self.rid, environ=env)
        child = self.spawn.calls[0][1]['env']
        self.assertEqual(child, {
            'SystemRoot': 'C:\\Windows', 'windir': 'C:\\Windows', 'SystemDrive': 'C:',
            'ComSpec': 'C:\\Windows\\system32\\cmd.exe', 'ProgramFiles': 'C:\\Program Files',
            'ProgramFiles(x86)': 'C:\\Program Files (x86)', 'ProgramW6432': 'C:\\Program Files',
            'CommonProgramFiles': 'C:\\Program Files\\Common Files',
            'CommonProgramFiles(x86)': 'C:\\x86\\Common Files',
            'ProgramData': 'C:\\ProgramData', 'ALLUSERSPROFILE': 'C:\\ProgramData',
            'PUBLIC': 'C:\\pub', 'USERPROFILE': 'D:\\home\\owner', 'HOMEDRIVE': 'D:',
            'HOMEPATH': '\\home\\owner', 'APPDATA': 'D:\\home\\owner\\roaming',
            'LOCALAPPDATA': 'D:\\home\\owner\\local', 'USERNAME': 'someone',
            'PATH': self.bin})

    def test_a_folder_the_registry_lacks_is_left_out_not_taken_from_the_caller(self):
        self.run_op(trigger=self.rid, environ={'USERPROFILE': 'C:\\evil', 'ComSpec': 'C:\\evil'})
        self.assertEqual(self.spawn.calls[0][1]['env'], {'PATH': self.bin})

    def test_a_folder_on_the_callers_path_is_never_searched(self):
        decoy = os.path.join(self.dir, 'decoy')
        os.makedirs(decoy)
        open(os.path.join(decoy, 'tool.exe'), 'wb').close()
        self.run_op(trigger=self.rid, environ=dict(self.environ, PATH=decoy))
        self.assertEqual(self.spawn.calls[0][1]['executable'], os.path.normpath(self.exe))
        self.spawn.calls.clear()
        self.reg.values.clear()
        self.add(decides='run D-001 name=beta')
        self.refused('in no absolute PATH folder', pairs=(('name', 'beta'),), trigger='H-kit-0003',
                     environ=dict(self.environ, PATH=decoy))

    def test_a_program_in_the_working_folder_is_never_started(self):
        here = os.path.join(self.dir, 'here')
        os.makedirs(here)
        open(os.path.join(here, 'tool.exe'), 'wb').close()
        cwd = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, cwd)
        self.reg.values[('HKLM', drun.MACHINE_ENV, 'Path')] = (
            os.pathsep.join(['', '.', 'here']), False)
        self.refused('in no absolute PATH folder', trigger=self.rid)

    def test_a_bat_is_refused(self):
        bat = os.path.join(self.bin, 'tool.bat')
        open(bat, 'wb').close()
        self.add(decides='run D-002 name=alpha')
        self.refused('is not an .exe', op=operation(id='D-002', command='tool.bat {name}'),
                     trigger='H-kit-0002')

    def test_a_relative_path_is_refused(self):
        self.add(decides='run D-002 name=alpha')
        self.refused('is a relative path', op=operation(id='D-002', command='bin/tool.exe {name}'),
                     trigger='H-kit-0002')

    def test_an_absolute_exe_is_started_as_it_is(self):
        self.add(decides='run D-002 name=alpha')
        self.run_op(op=operation(id='D-002', command=self.exe.replace('\\', '/') + ' {name}'),
                    trigger='H-kit-0002', registry=Registry())
        self.assertEqual(self.spawn.calls[0][1]['executable'], os.path.normpath(self.exe))

    def test_kit_is_the_kit_folder(self):
        self.add(decides='run D-002 name=alpha')
        kit = os.path.join(self.dir, 'kit')
        os.makedirs(kit)
        open(os.path.join(kit, 'tool.exe'), 'wb').close()
        self.run_op(op=operation(id='D-002', command='<kit>/tool.exe {name}'), trigger='H-kit-0002',
                    kit_root=kit)
        argv, kw = self.spawn.calls[0]
        self.assertEqual(argv, [os.path.normpath(os.path.join(kit, 'tool.exe')), 'alpha'])
        self.assertEqual(kw['cwd'], kit)

    def test_a_program_that_cannot_start_keeps_its_claim(self):
        self.spawn.error = OSError('no')
        res = self.run_op(trigger=self.rid)
        self.assertIsNone(res.returncode)
        self.assertIn('could not start', res.error)
        self.assertIn('## H-kit-0002 |', self.text())


class Triggers(Fixture):

    def test_session_is_refused(self):
        self.refused('give the owner ledger id', trigger='session')

    def test_a_standing_trigger_is_refused(self):
        self.refused('standing triggers are not run yet', trigger='after-landing')
        self.refused('standing triggers are not run yet', trigger='standing:pm-start-checklist')

    def test_something_else_is_refused(self):
        self.refused('is not a trigger', trigger='D-001')

    def test_an_owner_entry_needs_owner_session(self):
        rid = self.add()
        self.refused('does not take an owner session order', op=operation(trigger=['hub-answer']),
                     trigger=rid)

    def test_an_owner_entry_must_decide_the_run_line(self):
        rid = self.add(decides='run D-001 name=beta')
        self.refused('not the run line', trigger=rid)

    def test_an_owner_entry_older_than_a_day_is_refused(self):
        rid = self.add(age=datetime.timedelta(hours=25))
        self.refused('more than 24 h old', trigger=rid)

    def test_an_owner_entry_in_the_future_is_refused(self):
        rid = self.add(age=datetime.timedelta(hours=-1))
        self.refused('in the future', trigger=rid)

    def test_a_delegated_entry_is_no_order(self):
        rid = self.add()
        lwrite.add(self.ledger, 'kit', 'delegated', 'q', 'a', 'run D-001 name=alpha',
                   'under %s: by the PM' % rid, when=stamp(datetime.timedelta(minutes=30)),
                   repos=self.cfg['ledger_repos'], split=True)
        self.refused("is not the owner's", trigger='H-kit-0002')

    def test_a_retired_entry_is_no_order(self):
        rid = self.add()
        lwrite.retire(self.ledger, rid, 'spent', 'done', repos=self.cfg['ledger_repos'], split=True)
        self.refused('is retired', trigger=rid)

    def test_an_owner_entry_of_another_repo_is_refused(self):
        rid = self.add(repo='pm')
        self.refused('is a ruling of repo pm', trigger=rid)

    def test_an_unknown_entry_is_refused(self):
        self.refused('H-kit-0042 is not in the ledger', trigger='H-kit-0042')

    def test_no_ledger_is_refused(self):
        rid = self.add()
        self.refused('no ledger_dir or ledger_path', trigger=rid, cfg={})


class HubTrigger(Fixture):
    """A question Q-1001 the owner answered with the run line, logged as H-kit-0001, the one
    live owner entry whose Source names it: the hub keeps no ledger id on the question."""

    LINE = 'run D-001 name=alpha'

    def setUp(self):
        Fixture.setUp(self)
        self.add(decides='the owner chose run D-001 name=alpha', prov='selected',
                 source='hub Q-1001 and Q-1002')
        self.q = {'id': 'Q-1001', 'status': 'logged', 'repo': 'kit',
                  'options': [{'label': self.LINE}, {'label': 'not now'}],
                  'answer': {'choice': 0, 'label': self.LINE, 'note': '', 'via': 'web',
                             'at': (NOW - datetime.timedelta(hours=1)).astimezone().isoformat()}}
        self.hub = Hub([self.q])

    def go(self, **kw):
        kw.setdefault('hub', self.hub)
        return self.run_op(trigger='Q-1001', **kw)

    def deny(self, needle, **kw):
        kw.setdefault('hub', self.hub)
        return self.refused(needle, trigger='Q-1001', **kw)

    def test_a_logged_answer_runs_once_and_is_logged(self):
        res = self.go()
        self.assertEqual(res.entry, 'H-kit-0002')
        self.assertIn('## H-kit-0002 | 2026-10-20 12:00Z | kit | delegated', self.text())
        self.assertIn('Source: under H-kit-0001: D-001 Q-1001', self.text())
        self.assertIn('Decides: run D-001 name=alpha', self.text())
        self.assertEqual(self.hub.roles, ['agent'])

    def test_an_irreversible_operation_is_refused(self):
        op = schema.Operation(*operation())._replace(reversible=False)
        self.deny('is not reversible', op=op)

    def test_an_operation_without_hub_answer_is_refused(self):
        self.deny('does not take a hub answer', op=operation(trigger=['owner-session']))

    def test_a_question_not_logged_is_refused(self):
        self.q['status'] = 'answered'
        self.deny('is not a logged hub question')

    def test_a_filed_question_is_refused_until_its_asker_logs_it(self):
        self.q['status'] = 'filed'
        self.q['record'] = {'repo': 'kit', 'id': 'EX-1'}
        self.deny('is filed in its record but not logged')
        self.deny('log it first: `hub.py questions log %s --if-version V`' % self.q['id'])

    def test_a_filed_run_line_question_is_never_marked_done_by_the_hub(self):
        # hub_order runs only a logged question: the hub's own done (kitlib.hubsite.push "Done by
        # the hub") would leave a filed run line no asker can log any more
        line = drun.run_line(schema.Operation(*operation()), {'name': 'alpha'})
        self.assertEqual(line, self.LINE)
        self.q.update(status='filed', filed_as='pulled', record={'repo': 'kit', 'id': 'EX-1'})
        self.assertFalse(hubpush.auto_done_ok(self.q))
        self.q['options'][0]['label'] = 'go ahead'      # the answer alone still carries it
        self.assertFalse(hubpush.auto_done_ok(self.q))
        self.q['answer']['label'] = 'go ahead'
        self.assertTrue(hubpush.auto_done_ok(self.q), 'with no run line the hub marks it done')
        self.deny('is filed in its record but not logged')

    def test_a_filed_question_once_logged_runs(self):
        self.q['record'] = {'repo': 'kit', 'id': 'EX-1'}
        self.q['filed_at'] = self.q['answer']['at']
        self.assertEqual(self.go().entry, 'H-kit-0002')

    def test_another_label_is_refused(self):
        self.q['answer'].update(choice=1, label='not now')
        self.deny('not the run line')

    def test_other_is_refused(self):
        self.q['answer'].update(choice='other', label='Other')
        self.deny('did not choose an option')

    def test_a_question_of_another_repo_is_refused(self):
        self.q['repo'] = 'pm'
        self.deny('is a question of repo pm')

    def test_an_old_answer_is_refused(self):
        self.q['answer']['at'] = (NOW - datetime.timedelta(hours=25)).isoformat()
        self.deny('more than 24 h old')

    def test_an_answer_time_without_offset_is_refused(self):
        self.q['answer']['at'] = '2026-10-20T11:00:00'
        self.deny('has no offset')

    def test_an_old_logged_question_still_carrying_a_ruling_is_found_by_source(self):
        self.q['ruling'] = 'H-kit-0099'     # an older hub stored an id; nothing reads it now
        self.assertEqual(self.go().entry, 'H-kit-0002')
        self.assertIn('Source: under H-kit-0001: D-001 Q-1001', self.text())

    def _no_owner_entry(self):
        lwrite.retire(self.ledger, 'H-kit-0001', 'spent', 'made for the test',
                      repos=self.cfg['ledger_repos'], split=True)

    def test_an_entry_that_is_not_the_owners_does_not_count(self):
        self._no_owner_entry()
        lwrite.add(self.ledger, 'kit', 'delegated', 'q', 'a', 'd',
                   'under H-kit-0001: hub %s' % self.q['id'],
                   when=stamp(datetime.timedelta(minutes=30)), repos=self.cfg['ledger_repos'], split=True)
        self.deny("no live owner ledger entry's Source names it")

    def test_a_source_naming_another_question_does_not_count(self):
        self._no_owner_entry()
        self.add(decides='x', prov='typed', source='hub %s2' % self.q['id'])    # a longer id, not this one
        self.deny("no live owner ledger entry's Source names it")

    def test_two_live_owner_entries_naming_the_question_are_refused(self):
        self.add(decides='x', prov='typed', source='hub %s again' % self.q['id'])
        self.deny('more than one live owner ledger entry names it in its Source '
                  '(H-kit-0001, H-kit-0002)')
        self.assertNotIn('delegated', self.text())

    def test_a_superseded_entry_does_not_count(self):
        lwrite.add(self.ledger, 'kit', 'typed', 'q', 'a', 'run D-001 name=alpha', 'hub Q-1001',
                   supersedes='H-kit-0001', when=stamp(datetime.timedelta(minutes=30)),
                   repos=self.cfg['ledger_repos'], split=True)
        self.assertEqual(self.go().entry, 'H-kit-0003')
        self.assertIn('Source: under H-kit-0002: D-001 Q-1001', self.text())

    def test_a_hub_that_does_not_answer_is_refused(self):
        self.deny('cannot read Q-1001 from the hub', hub=Hub(error=hubserve.NotRunning(8765)))
        self.assertNotIn('delegated', self.text())

    def test_the_question_runs_once(self):
        self.go()
        self.assertEqual(len(self.spawn.calls), 1)
        self.spawn.calls.clear()
        self.deny('already ran')

    def test_its_ruling_orders_one_run(self):
        self.go()
        # A second question logged under the same ruling is the same order.
        q2 = dict(self.q, id='Q-1002')
        self.hub.questions.append(q2)
        self.spawn.calls.clear()
        self.refused('already ran', trigger='Q-1002', hub=self.hub)


class UseOnce(Fixture):

    def setUp(self):
        Fixture.setUp(self)
        self.rid = self.add()

    def test_an_owner_entry_runs_once(self):
        self.run_op(trigger=self.rid)
        self.spawn.calls.clear()
        self.refused('already ran', trigger=self.rid)

    def test_a_failed_run_keeps_its_claim(self):
        self.spawn.code = 3
        res = self.run_op(trigger=self.rid)
        self.assertEqual(res.returncode, 3)
        self.spawn.calls.clear()
        self.spawn.code = 0
        self.refused('already ran', trigger=self.rid)

    def test_the_claim_is_written_before_the_program_starts(self):
        seen = []
        self.spawn.check = lambda argv, kw: seen.append('Source: under %s: D-001 %s'
                                                        % (self.rid, self.rid) in self.text())
        self.run_op(trigger=self.rid)
        self.assertEqual(seen, [True])

    def test_two_runs_at_once_claim_once(self):
        inside, out = threading.Event(), {}

        def pause():
            inside.set()
            time.sleep(0.5)             # the other run reaches its scan meanwhile

        def one(name, **kw):
            try:
                out[name] = self.run_op(trigger=self.rid, **kw).entry
            except drun.RunRefused as e:
                out[name] = 'refused: %s' % e
        a = threading.Thread(target=one, args=('a',), kwargs={'pause': pause})
        a.start()
        self.assertTrue(inside.wait(10))
        b = threading.Thread(target=one, args=('b',))
        b.start()
        a.join(30)
        b.join(30)
        self.assertEqual(out['a'], 'H-kit-0002', out)
        self.assertIn('already ran', out['b'], out)
        self.assertEqual(self.text().count('| delegated'), 1)
        self.assertEqual(len(self.spawn.calls), 1)

    def test_a_ledger_refusal_starts_nothing(self):
        with open(self.ledger, 'a', encoding='utf-8') as fh:
            fh.write('\n## H-kit-0009 | broken\n')
        self.refused('the ledger refused the run entry', trigger=self.rid)

    def test_a_one_file_ledger_logs_in_it(self):
        path = os.path.join(self.dir, 'LEDGER.md')
        cfg = {'ledger_path': path, 'ledger_repos': ['kit', 'pm']}
        rid = lwrite.add(path, 'kit', 'typed', 'q', 'a', 'run D-001 name=alpha', 'the owner',
                         when=stamp(datetime.timedelta(hours=1)), repos=cfg['ledger_repos']).id
        res = self.run_op(trigger=rid, cfg=cfg)
        self.assertNotEqual(res.entry, rid)
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        self.assertIn('## %s | 2026-10-20 12:00Z | kit | delegated' % res.entry, text)
        self.assertIn('Source: under %s: D-001 %s' % (rid, rid), text)
        self.assertNotIn('delegated', self.text())
        self.spawn.calls.clear()
        self.refused('already ran', trigger=rid, cfg=cfg)

    def test_a_repo_with_no_ledger_file_is_refused(self):
        self.add(decides='run D-001 name=alpha', repo='kit')
        cfg = {'ledger_dir': os.path.join(self.dir, 'split'), 'ledger_repos': ['pm']}
        self.refused('is not one of ledger_repos', trigger=self.rid, cfg=cfg)


class Preconditions(Fixture):

    def setUp(self):
        Fixture.setUp(self)
        self.rid = self.add()

    def op(self, *pres):
        return operation(preconditions=list(pres))

    def test_a_missing_file_is_refused(self):
        path = os.path.join(self.dir, 'alpha.txt').replace('\\', '/')
        self.refused('is not a file', op=self.op({'kind': 'file-exists', 'path': path}),
                     trigger=self.rid, checks=drun.CHECKS)
        self.assertNotIn('delegated', self.text())

    def test_a_file_named_by_an_argument_is_checked(self):
        open(os.path.join(self.dir, 'alpha.txt'), 'w').close()
        path = self.dir.replace('\\', '/') + '/{name}.txt'
        self.run_op(op=self.op({'kind': 'file-exists', 'path': path}), trigger=self.rid,
                    checks=drun.CHECKS)
        self.assertEqual(len(self.spawn.calls), 1)

    def test_a_check_that_fails_is_refused_before_the_claim(self):
        self.refused('does not hold: no', op=self.op({'kind': 'checklist-line', 'text': 'x'}),
                     trigger=self.rid, checks={'checklist-line': lambda pre: 'no'})
        self.assertNotIn('delegated', self.text())

    def test_a_checklist_line_is_read_from_the_pm_start_checklist(self):
        text = 'PM session start checklist:\n- hub key: kit\n- could not check cap: kit x'
        with mock.patch('kitlib.pmstart.load_config', return_value={}), \
                mock.patch('kitlib.pmstart.checklist', return_value=text):
            self.assertIsNone(drun.checklist_line({'kind': 'checklist-line', 'text': 'hub key'}))
            self.assertIn('no PM start checklist line',
                          drun.checklist_line({'kind': 'checklist-line', 'text': 'cap: kit'}))

    def test_no_pm_start_config_is_refused(self):
        with mock.patch('kitlib.pmstart.load_config', return_value=None):
            self.assertIn('no pm_start config', drun.checklist_line({'text': 'x'}))


class BranchLanded(unittest.TestCase):
    """branch_landed on a real repository and its bare origin."""

    def setUp(self):
        env = mock.patch.dict(os.environ, gitfixture.fixture_env(), clear=True)
        env.start()
        self.addCleanup(env.stop)

        def prepare(fx):
            fx.git('switch', '-q', '-c', 'topic')
            fx.write('b.txt', 'topic work\n')
            fx.commit('topic work', 'b.txt')
            fx.git('switch', '-q', 'main')
        self.fx = gitfixture.TempRepo(branch='main', bare_remote=True, prepare=prepare,
                                      prepare_key=('test_run.BranchLanded',))
        self.addCleanup(self.fx.cleanup)
        self.root = os.path.realpath(self.fx.root)
        tmp = os.path.realpath(self.fx.base)
        os.environ['LOCALAPPDATA'] = os.path.join(tmp, 'localappdata')
        temp = os.path.join(tmp, 'localappdata', 'Temp')
        os.makedirs(temp)
        for patch in (mock.patch.object(tempfile, 'tempdir', temp),
                      mock.patch.object(kuser, 'home_dir', return_value=tmp)):
            patch.start()
            self.addCleanup(patch.stop)

    def check(self, branch='topic'):
        return drun.branch_landed({'kind': 'branch-landed', 'repo_path': self.root, 'branch': branch})

    def land(self):
        self.fx.git('merge', '-q', '--no-ff', '-m', 'merge', 'topic', cwd=self.root)
        self.fx.git('push', '-q', 'origin', 'main', cwd=self.root)

    def test_an_unlanded_branch_does_not_hold(self):
        self.assertIn('has not landed', self.check())

    def test_a_landed_branch_holds(self):
        self.land()
        self.assertIsNone(self.check())

    def test_a_deleted_branch_fails_closed(self):
        self.land()
        self.fx.git('branch', '-q', '-D', 'topic', cwd=self.root)
        self.assertIn('is no branch', self.check())

    def test_a_remote_tracking_branch_is_read(self):
        self.land()
        self.fx.git('push', '-q', 'origin', 'topic', cwd=self.root)
        self.fx.git('fetch', '-q', 'origin', cwd=self.root)
        self.fx.git('branch', '-q', '-D', 'topic', cwd=self.root)
        self.assertIsNone(self.check())


class RegistryEnv(unittest.TestCase):
    """registry_env: PATH, PATHEXT and PSModulePath from the registry, never the caller's."""

    M, U, V = ('HKLM', drun.MACHINE_ENV), ('HKCU', drun.USER_ENV), ('HKCU', drun.VOLATILE_ENV)

    def reg(self, where, name, value, expandable=False):
        return {where + (name,): (value, expandable)}

    def test_the_path_is_the_machines_then_the_users(self):
        values = self.reg(self.M, 'Path', 'C:\\m1;C:\\m2') | self.reg(self.U, 'Path', 'C:\\u')
        self.assertEqual(drun.registry_env(Registry(values))['PATH'], 'C:\\m1;C:\\m2;C:\\u')

    def test_a_name_is_filled_from_the_registry_not_the_caller(self):
        values = {}
        values.update(self.reg(self.M, 'Path', '%SystemRoot%\\system32;%JAVA_HOME%\\bin', True))
        values.update(self.reg(self.U, 'Path', '%USERPROFILE%\\bin;%SystemDrive%\\tools', True))
        values[('HKLM', drun.NT_CURRENT, 'SystemRoot')] = ('C:\\Windows', False)
        values[self.V + ('USERPROFILE',)] = ('C:\\profiles\\someone', False)
        values.update(self.reg(self.M, 'JAVA_HOME', '%ProgramFiles%\\java', True))
        values[('HKLM', drun.WIN_CURRENT, 'ProgramFilesDir')] = ('C:\\Program Files', False)
        with mock.patch.dict(os.environ, {'SystemRoot': 'C:\\evil', 'USERPROFILE': 'C:\\evil',
                                          'JAVA_HOME': 'C:\\evil', 'ProgramFiles': 'C:\\evil'}):
            path = drun.registry_env(Registry(values))['PATH']
        self.assertEqual(path, 'C:\\Windows\\system32;C:\\Program Files\\java\\bin;'
                               'C:\\profiles\\someone\\bin;C:\\tools')

    def test_an_entry_with_a_name_the_registry_lacks_is_dropped(self):
        values = self.reg(self.M, 'Path', 'C:\\a;%NOPE%\\bin;C:\\%NOPE%;C:\\b', True)
        with mock.patch.dict(os.environ, {'NOPE': 'C:\\evil'}):
            self.assertEqual(drun.registry_env(Registry(values))['PATH'], 'C:\\a;C:\\b')

    def test_a_loop_of_names_is_dropped(self):
        values = self.reg(self.M, 'Path', 'C:\\a;%X%', True) | self.reg(self.M, 'X', '%X%', True)
        self.assertEqual(drun.registry_env(Registry(values))['PATH'], 'C:\\a')

    def test_a_plain_value_is_not_expanded(self):
        values = self.reg(self.M, 'Path', 'C:\\%SystemRoot%', False)
        self.assertEqual(drun.registry_env(Registry(values))['PATH'], 'C:\\%SystemRoot%')

    def test_pathext_is_the_users_or_else_the_machines(self):
        m = self.reg(self.M, 'PATHEXT', '.COM;.EXE')
        self.assertEqual(drun.registry_env(Registry(m))['PATHEXT'], '.COM;.EXE')
        both = m | self.reg(self.U, 'PATHEXT', '.EXE')
        self.assertEqual(drun.registry_env(Registry(both))['PATHEXT'], '.EXE')

    def test_psmodulepath_is_the_machines_then_the_users(self):
        values = self.reg(self.M, 'PSModulePath', 'C:\\m') | self.reg(self.U, 'PSModulePath', 'C:\\u')
        self.assertEqual(drun.registry_env(Registry(values))['PSModulePath'], 'C:\\m;C:\\u')

    def test_no_values_give_no_names(self):
        self.assertEqual(drun.registry_env(Registry()), {})

    def test_the_registry_is_not_read_under_the_tests(self):
        self.assertTrue(kuser.fenced())
        self.assertIsNone(drun.read_registry('HKLM', drun.MACHINE_ENV, 'Path'))

    def test_the_real_registry_read_tells_plain_from_expandable(self):
        # Read only, and only values every Windows has: the fence is lifted for this read alone.
        with mock.patch.object(kuser, 'fenced', return_value=False):
            root = drun.read_registry('HKLM', drun.NT_CURRENT, 'SystemRoot')
            comspec = drun.read_registry('HKLM', drun.MACHINE_ENV, 'ComSpec')
            pf = drun.read_registry('HKLM', drun.WIN_CURRENT, 'ProgramFilesDir')
            pf64 = drun.read_registry('HKLM', drun.WIN_CURRENT, 'ProgramW6432Dir')
            dword = drun.read_registry('HKLM', drun.NT_CURRENT, 'CurrentMajorVersionNumber')
            missing = drun.read_registry('HKLM', drun.NT_CURRENT, 'kit-no-such-value')
        self.assertIs(root[1], False)
        self.assertTrue(os.path.isdir(root[0]), root)
        self.assertIs(comspec[1], True)
        self.assertIn('%SystemRoot%', comspec[0])
        if pf64 is not None:                # a 64-bit Windows: the 64-bit view is read
            self.assertEqual(pf, pf64)
        self.assertIsNone(dword)            # a REG_DWORD is no string
        self.assertIsNone(missing)


class ApprovalHub(object):
    """A stand-in hub client for an op approval: `get` answers verify, `post` answers consume.
    Every call goes in `events`, shared with the stand-in checks, cwd and spawn."""

    def __init__(self, events, verdict=None, consumed=None, verify_error=None, consume_error=None):
        self.events, self.calls = events, []
        self.verdict = verdict if verdict is not None else {'ok': True, 'valid': True, 'why': '',
                                                           'version': 3}
        self.consumed = consumed if consumed is not None else {'status': 'consumed'}
        self.verify_error, self.consume_error = verify_error, consume_error

    def get(self, path, role=None, query=None, timeout=None):
        self.events.append('verify')
        self.calls.append(('get', path, role, query))
        if self.verify_error:
            raise self.verify_error
        return self.verdict

    def post(self, path, role, body=None):
        self.events.append('consume')
        self.calls.append(('post', path, role, body))
        if self.consume_error:
            raise self.consume_error
        return self.consumed


class HubApproval(Fixture):
    """A run on a hub approval `hub:A-nnn` (docs/delegations.md "Hub approvals")."""

    def setUp(self):
        Fixture.setUp(self)
        self.dir = os.path.realpath(self.dir)
        self.dev = os.path.join(self.dir, 'dd')
        self.home = os.path.join(self.dir, 'home')
        self.kit = os.path.join(self.dir, 'kit')
        for d in (self.dev, self.home, self.kit):
            os.makedirs(d)
        self.cfg = dict(self.cfg, dev_drive=self.dev,
                        plugin={'export_dir': os.path.join(self.dir, 'release')})
        self.dest = os.path.join(self.dev, 'out')
        self.line = 'run D-001 dest=%s' % self.dest
        self.events = []
        self.hub = ApprovalHub(self.events)
        self.cwds = []
        self.spawn = Spawn(check=lambda argv, kw: self.events.append('spawn'))

    def op(self, **kw):
        kw.setdefault('command', 'tool {dest}')
        kw.setdefault('args', [{'name': 'dest', 'pattern': '[A-Za-z0-9:/\\\\._-]+'}])
        kw.setdefault('trigger', ['hub-approval'])
        return operation(**kw)

    def make_cwd(self):
        self.events.append('cwd')
        folder = tempfile.mkdtemp(prefix='kit-delegated-cwd-', dir=self.dir)
        self.cwds.append(folder)
        return folder

    def check_pre(self, pre):
        self.events.append('precondition')
        return None

    def approve(self, approval='hub:A-001', pairs=None, **kw):
        kw.setdefault('op', self.op(preconditions=[{'kind': 'file-exists', 'path': self.exe}]))
        kw.setdefault('hub', self.hub)
        kw.setdefault('home', self.home)
        kw.setdefault('kit_root', self.kit)
        kw.setdefault('make_cwd', self.make_cwd)
        kw.setdefault('checks', {'file-exists': self.check_pre})
        return self.run_op(pairs=pairs or (('dest', self.dest),), trigger=approval, **kw)

    def refused_approval(self, needle, **kw):
        with self.assertRaises(drun.RunRefused) as cm:
            self.approve(**kw)
        self.assertIn(needle, str(cm.exception))
        self.assertEqual(self.spawn.calls, [], 'a refused run started a program')
        self.assertFalse(os.path.exists(self.ledger), 'a refused run wrote the ledger')
        return str(cm.exception)

    def test_the_run_is_verified_checked_consumed_claimed_then_started(self):
        res = self.approve()
        self.assertEqual(self.events, ['verify', 'precondition', 'cwd', 'consume', 'spawn'])
        self.assertEqual((res.returncode, res.error), (0, None))
        self.assertEqual(res.argv, [os.path.normpath(self.exe), self.dest])
        sha = hubapprovals.line_sha256(self.line)
        (_, vpath, vrole, query), (_, cpath, crole, body) = self.hub.calls
        self.assertEqual((vpath, vrole), ('/approvals/A-001/verify', hubclient.AGENT))
        self.assertEqual(query, {'kind': 'op', 'op': 'D-001', 'repo': 'kit', 'args': sha})
        self.assertEqual((cpath, crole), ('/approvals/A-001/consume', hubclient.AGENT))
        self.assertEqual(body, {'args_sha256': sha, 'if_version': 3})

    def entries(self):
        text = self.text()
        self.assertEqual(drun.fmt.validate(text, repo='kit'), [])
        return [drun.fmt.parse_block(b)[0] for _, b in drun.fmt.split_entries(text)[1]]

    def test_the_run_entry_is_delegated_under_the_approval_and_no_owner_entry_is_written(self):
        res = self.approve()
        entries = self.entries()
        self.assertEqual([e['prov'] for e in entries], ['delegated'], 'one entry, delegated')
        self.assertEqual([e for e in entries if e['prov'] in drun.fmt.OWNER_PROVS], [])
        ran = entries[0]
        self.assertEqual((ran['id'], ran['prov'], ran['Decides']), (res.entry, 'delegated', self.line))
        self.assertEqual(ran['Source'], 'under hub:A-001: D-001 hub:A-001')
        self.assertEqual(ran['A'], "the owner approved this run on the hub as hub:A-001: the hub "
                                   "verified the owner's signed approval for D-001 on kit and this "
                                   "run line, and recorded its use")
        texts = drun.ledger_texts(drun.lfiles.of_config(self.cfg))
        self.assertEqual(drun.used(texts, None, 'hub:A-001'), 'H-kit-0001')
        self.assertIsNone(drun.used(texts, None, 'hub:A-002'))

    def test_with_ledger_dir_the_entry_goes_to_the_ops_repo_file_never_the_frozen_one(self):
        frozen = os.path.join(self.dir, 'LEDGER.md')
        with open(frozen, 'w', encoding='utf-8') as fh:
            fh.write('frozen\n')
        self.cfg = dict(self.cfg, ledger_path=frozen)
        self.approve(op=self.op(repo='*', preconditions=[{'kind': 'file-exists', 'path': self.exe}]))
        with open(os.path.join(self.folder, 'pm.md'), encoding='utf-8') as fh:
            text = fh.read()
        self.assertEqual(drun.fmt.validate(text, repo='pm'), [])
        self.assertIn('## H-pm-0001 | 2026-10-20 12:00Z | pm | delegated', text)
        self.assertIn('Source: under hub:A-001: D-001 hub:A-001', text)
        self.assertNotIn('H-pm-0002', text)
        for prov in drun.fmt.OWNER_PROVS:
            self.assertNotIn('| %s\n' % prov, text)
        self.assertFalse(os.path.exists(self.ledger), 'the kit file was written')
        with open(frozen, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), 'frozen\n')

    def test_a_source_under_the_approval_is_a_runner_entry_and_a_malformed_one_is_not(self):
        m = drun.SOURCE_RE.match('under hub:A-001: D-001 hub:A-001')
        self.assertEqual(m.groups(), ('hub:A-001', 'D-001', 'hub:A-001'))
        for bad in ('under hub:A-01: D-001 hub:A-01', 'under A-001: D-001 hub:A-001',
                    'under hub:A-000: D-001 hub:A-000', 'under hub:A-0001: D-001 hub:A-0001'):
            self.assertIsNone(drun.SOURCE_RE.match(bad), bad)

    def test_a_used_approval_is_refused_before_the_hub_is_asked(self):
        self.approve()
        self.events.clear()
        self.spawn.calls.clear()
        with self.assertRaises(drun.RunRefused) as cm:
            self.approve()
        self.assertIn('hub:A-001 was used already by H-kit-0001', str(cm.exception))
        self.assertEqual(self.events, [])
        self.assertEqual(self.spawn.calls, [])

    def add_under_approval(self):
        found = drun.lfiles.of_config(self.cfg)
        col, path, split = drun.target(found, 'kit')
        drun.lwrite.add(path, col, 'delegated', 'q', 'a', self.line, 'under hub:A-001 by hand',
                        when='2026-10-20 11:00Z', repos=self.cfg.get('ledger_repos'), split=split)
        return found, path, split

    def test_any_live_entry_under_the_approval_refuses_it(self):
        self.add_under_approval()
        with self.assertRaises(drun.RunRefused) as cm:
            self.approve()
        self.assertIn('hub:A-001 was used already by H-kit-0001', str(cm.exception))
        self.assertEqual(self.events, [])
        self.assertEqual(self.spawn.calls, [])

    def test_a_retired_entry_under_the_approval_does_not_count(self):
        found, path, split = self.add_under_approval()
        drun.lwrite.retire(path, 'H-kit-0001', 'spent', 'done', repos=self.cfg['ledger_repos'],
                           split=split)
        texts = drun.ledger_texts(found)
        self.assertIn('Retired: spent', self.text())
        self.assertIsNone(drun.used(texts, None, 'hub:A-001'))

    def test_the_run_entry_is_no_owner_order(self):
        # The approval leaves no owner entry: the run entry it wrote is delegated, so it never
        # passes as an owner-session order for an op that also lists owner-session.
        op = self.op(trigger=['owner-session', 'hub-approval'],
                     preconditions=[{'kind': 'file-exists', 'path': self.exe}])
        self.approve(op=op)
        ran, = self.entries()
        self.events.clear()
        self.spawn.calls.clear()
        with self.assertRaises(drun.RunRefused) as cm:
            self.run_op(op=op, pairs=(('dest', self.dest),), trigger=ran['id'],
                        kit_root=self.kit, home=self.home, make_cwd=self.make_cwd,
                        checks={'file-exists': self.check_pre})
        self.assertIn("%s is not the owner's: its provenance is delegated" % ran['id'],
                      str(cm.exception))
        self.assertEqual(self.spawn.calls, [])
        self.assertEqual(len(self.entries()), 1)

    def test_the_child_runs_in_a_new_empty_folder_removed_when_left_empty(self):
        self.approve()
        cwd = self.spawn.calls[0][1]['cwd']
        self.assertEqual(self.cwds, [cwd])
        self.assertNotEqual(os.path.normcase(cwd), os.path.normcase(self.kit))
        self.assertFalse(os.path.exists(cwd))

    def test_a_folder_the_child_wrote_in_is_kept(self):
        def write(argv, kw):
            open(os.path.join(kw['cwd'], 'made.txt'), 'w').close()
        self.spawn = Spawn(check=write)
        self.approve()
        self.assertTrue(os.path.isfile(os.path.join(self.cwds[0], 'made.txt')))

    def test_the_default_folder_is_under_the_machine_temp_folder(self):
        with mock.patch.object(kuser, 'mkdtemp', side_effect=lambda prefix, home=None: (
                self.events.append(('mkdtemp', prefix, home)) or self.make_cwd())):
            self.approve(make_cwd=None)
        self.assertIn(('mkdtemp', drun.CWD_PREFIX, self.home), self.events)

    def test_an_approval_of_another_form_is_refused(self):
        for bad in ('hub:A-000', 'hub:A-1', 'hub:A-0001', 'hub:B-001', 'hub:A-001 ', 'hub:'):
            with self.subTest(bad=bad):
                self.refused_approval('--approval takes hub:A-nnn, not %r' % bad, approval=bad)
        self.assertEqual(self.events, [])
        self.assertEqual(drun.trigger_kind('hub:A-1000'), drun.APPROVAL)

    def test_an_op_that_does_not_list_hub_approval_is_refused(self):
        msg = self.refused_approval('D-001 does not list hub-approval among its triggers',
                                    op=self.op(trigger=['owner-session']))
        self.assertIn('owner-session', msg)
        self.assertEqual(self.events, [])

    def test_a_path_off_the_dev_drive_is_refused_before_the_hub_is_asked(self):
        other = os.path.join(self.dir, 'other')
        msg = self.refused_approval('D-001 on hub:A-001 would act where no approved run may',
                                    pairs=(('dest', other),))
        self.assertIn(place.WHY_OFF % (other, self.dev), msg)
        self.assertEqual(self.events, [])

    def test_a_path_in_the_command_itself_is_judged(self):
        repo = os.path.join(self.dev, 'repo')
        os.makedirs(os.path.join(repo, '.git'))
        op = self.op(command='tool {dest} /LOG:%s' % os.path.join(repo, 'x.log'))
        msg = self.refused_approval('would act where no approved run may', op=op)
        self.assertIn('inside the checkout %s' % repo, msg)
        self.assertEqual(self.events, [])

    def test_no_dev_drive_refuses(self):
        self.cfg['dev_drive'] = None
        self.refused_approval('sets no dev_drive')
        self.assertEqual(self.events, [])

    def test_no_hub_refuses(self):
        self.refused_approval('no hub to verify hub:A-001 on', hub=None)

    def test_a_hub_verdict_against_it_is_refused(self):
        for why in (hubapprovals.WHY_ARGS, hubapprovals.WHY_OP, hubapprovals.WHY_REPO,
                    hubapprovals.WHY_KIND_FILES, hubapprovals.WHY_STATUS % 'consumed'):
            with self.subTest(why=why):
                self.events.clear()
                self.hub.verdict = {'ok': True, 'valid': False, 'why': why}
                self.refused_approval('the hub refused hub:A-001: %s' % why)
                self.assertEqual(self.events, ['verify'])

    def test_a_hub_that_cannot_answer_is_refused(self):
        self.hub.verify_error = hubserve.ServeError('down')
        msg = self.refused_approval('the hub cannot be reached: ')
        self.assertIn('nothing was run', msg)
        self.assertEqual(self.events, ['verify'])

    def test_a_verdict_with_no_version_is_refused(self):
        self.hub.verdict = {'ok': True, 'valid': True, 'why': ''}
        self.refused_approval('the hub refused hub:A-001: %s' % hubapprovals.WHY_ANSWER)

    def test_a_failed_precondition_consumes_nothing(self):
        self.refused_approval('precondition 0 (file-exists) does not hold',
                              checks={'file-exists': lambda pre: self.events.append('pre') or 'no'})
        self.assertEqual(self.events, ['verify', 'pre'])

    def test_a_folder_that_cannot_be_made_consumes_nothing(self):
        def broken():
            raise OSError('disk full')
        self.refused_approval('cannot make the empty working folder: disk full', make_cwd=broken)
        self.assertEqual(self.events, ['verify', 'precondition'])

    def test_a_consume_that_fails_runs_and_logs_nothing(self):
        for error in (hubserve.ServeError('409 conflict'), OSError('reset'), ValueError('bad')):
            with self.subTest(error=error):
                self.events.clear()
                self.cwds.clear()
                self.hub.consume_error = error
                msg = self.refused_approval('the hub did not record the use of hub:A-001: %s'
                                            % error)
                self.assertIn('nothing was run', msg)
                self.assertEqual(self.events, ['verify', 'precondition', 'cwd', 'consume'])
                self.assertFalse(os.path.exists(self.cwds[0]))

    def test_a_consume_answering_another_status_runs_nothing(self):
        for answer in ({'status': 'approved'}, {}, ['consumed'], {'status': None}):
            with self.subTest(answer=answer):
                self.hub.consumed = answer
                self.refused_approval('the hub did not record the use of hub:A-001: it answered')

    def test_a_question_or_ledger_trigger_still_runs_in_the_kit_folder(self):
        rid = self.add(decides=self.line)
        self.run_op(op=self.op(trigger=['owner-session']), pairs=(('dest', self.dest),),
                    trigger=rid, kit_root=self.kit, home=self.home, make_cwd=self.make_cwd)
        self.assertEqual(self.spawn.calls[0][1]['cwd'], self.kit)
        self.assertEqual(self.cwds, [])


class RequestHub(object):
    """A stand-in hub client for request: `post` answers `reply` (or raises `error`)."""

    def __init__(self, reply=None, error=None):
        self.reply = {'ok': True, 'id': 'A-007'} if reply is None else reply
        self.error, self.calls = error, []

    def post(self, path, role, body=None):
        self.calls.append((path, role, body))
        if self.error:
            raise self.error
        return self.reply


class Request(Fixture):
    """request: the owner is asked on the hub only for a run the runner could take."""

    op = HubApproval.op

    def ask(self, hub='default', pairs=None, **kw):
        op = kw.pop('op', None) or self.op(what='copy the build out')
        return drun.request(op, list(pairs or (('dest', self.dest),)),
                            kw.pop('cfg', self.cfg), self.hub_r if hub == 'default' else hub,
                            'Copy the build', reason=kw.pop('reason', 'for the demo'),
                            environ=self.environ, kit_root=self.kit, registry=self.reg,
                            home=self.home, **kw)

    def setUp(self):
        HubApproval.setUp(self)
        self.hub_r = RequestHub()

    def refused_request(self, needle, **kw):
        with self.assertRaises(drun.RunRefused) as cm:
            self.ask(**kw)
        self.assertIn(needle, str(cm.exception))
        return str(cm.exception)

    def test_a_request_posts_the_run_line_and_answers_the_new_id(self):
        self.assertEqual(self.ask(), 'A-007')
        self.assertEqual(self.hub_r.calls, [('/approvals', hubclient.AGENT, {
            'kind': 'op', 'op': 'D-001', 'repo': 'kit', 'line': self.line,
            'title': 'Copy the build', 'reason': 'for the demo', 'what': 'copy the build out'})])
        self.assertFalse(os.path.exists(self.ledger), 'a request wrote the ledger')
        self.assertEqual(self.spawn.calls, [])

    def test_a_request_off_the_dev_drive_is_never_asked(self):
        off = os.path.join(self.dir, 'elsewhere')
        why = self.refused_request('would act where no approved run may', pairs=(('dest', off),))
        self.assertIn('not on the Dev Drive', why)
        self.assertEqual(self.hub_r.calls, [])

    def test_a_request_for_an_op_without_hub_approval_is_never_asked(self):
        self.refused_request('does not list hub-approval',
                             op=self.op(trigger=['owner-session']))
        self.assertEqual(self.hub_r.calls, [])

    def test_a_request_with_a_bad_argument_is_never_asked(self):
        self.refused_request("dest: 'a b' does not match", pairs=(('dest', 'a b'),))
        self.assertEqual(self.hub_r.calls, [])

    def test_a_request_with_no_dev_drive_is_never_asked(self):
        cfg = dict(self.cfg)
        del cfg['dev_drive']
        self.refused_request('sets no dev_drive', cfg=cfg)
        self.assertEqual(self.hub_r.calls, [])

    def test_no_hub_is_refused(self):
        self.refused_request('no hub to ask on', hub=None)

    def test_a_hub_refusal_is_refused(self):
        for error in (hubserve.ServeError('409 invalid line'), OSError('down'),
                      ValueError('not json')):
            with self.subTest(error=type(error).__name__):
                self.refused_request('the hub did not take the request',
                                     hub=RequestHub(error=error))

    def test_a_reply_without_an_id_is_refused(self):
        for reply in ({'ok': True}, {'id': ''}, ['A-001']):
            with self.subTest(reply=reply):
                self.refused_request("answered without the new approval's id",
                                     hub=RequestHub(reply=reply))


if __name__ == '__main__':
    unittest.main()
