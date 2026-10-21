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
from kitlib.delegations import run as drun
from kitlib.delegations import schema
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
        self.environ = {'PATH': self.bin, 'SYSTEMROOT': 'C:\\Windows'}
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

    def test_the_child_environment_has_no_python_variable(self):
        env = dict(self.environ, PYTHONPATH='C:/evil', PythonHome='C:/evil', PYTHONSTARTUP='x.py',
                   PYTHONINSPECT='1', PYTHONUTF8='1', KEEP='me')
        self.run_op(trigger=self.rid, environ=env)
        child = self.spawn.calls[0][1]['env']
        self.assertEqual(sorted(k for k in child if k.upper().startswith('PYTHON')), [])
        self.assertEqual(child['KEEP'], 'me')

    def test_a_program_in_the_working_folder_is_never_started(self):
        here = os.path.join(self.dir, 'here')
        os.makedirs(here)
        open(os.path.join(here, 'tool.exe'), 'wb').close()
        cwd = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, cwd)
        self.refused('in no absolute PATH folder', trigger=self.rid,
                     environ={'PATH': os.pathsep.join(['', '.', 'here'])})

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
                    trigger='H-kit-0002', environ={'PATH': ''})
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
    """A question Q-1001 the owner answered with the run line, logged as H-kit-0001."""

    LINE = 'run D-001 name=alpha'

    def setUp(self):
        Fixture.setUp(self)
        self.add(decides='the owner chose run D-001 name=alpha', prov='selected',
                 source='hub Q-1001 and Q-1002')
        self.q = {'id': 'Q-1001', 'status': 'logged', 'repo': 'kit', 'ruling': 'H-kit-0001',
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

    def test_a_ruling_that_is_not_the_owners_is_refused(self):
        lwrite.add(self.ledger, 'kit', 'delegated', 'q', 'a', 'd', 'under H-kit-0001: hub Q-1001',
                   when=stamp(datetime.timedelta(minutes=30)), repos=self.cfg['ledger_repos'], split=True)
        self.q['ruling'] = 'H-kit-0002'
        self.deny("is not the owner's")

    def test_a_ruling_whose_source_does_not_name_the_question_is_refused(self):
        self.add(decides='x', prov='typed', source='hub Q-10012')
        self.q['ruling'] = 'H-kit-0002'
        self.deny('does not name Q-1001')

    def test_a_ruling_that_is_no_id_is_refused(self):
        self.q['ruling'] = 'yes'
        self.deny('is not a ledger id')

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


if __name__ == '__main__':
    unittest.main()
