"""kitlib.autotasks.tick and tools/auto_tasks.py: the state file, the fixed task table, the hub-down
path and the double-start guard. The hub, the launcher repositories and the program start are
fakes; the home folder is a temp folder, so the state file is never the machine's."""
import contextlib
import datetime
import io
import json
import os
import socket
import tempfile
import unittest
from unittest import mock

from kitlib.autotasks import tick as T
from kitlib.fs.treelock import TreeLock
from kitlib.hubsite import serve
from tests import gitfixture
from tests.home import machine_config, temp_home
from tests.realgit import real_git

UNIGNORED = T.unignored     # Base puts a fake in its place
D = datetime.datetime
NOW = D(2026, 10, 21, 12, 0)
PY = 'C:\\Py\\python.exe'
MAIN = os.path.normpath('C:/r/demo')


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'w').close()


def sched(task='worktree-sweep', repo='demo', cadence='daily', at='09:00', **kw):
    s = {'id': '%s-%s' % (task, repo), 'task': task, 'repo': repo, 'cadence': cadence, 'at': at}
    s.update(kw)
    return s


class Base(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.pm_folder = machine_config(self.home, {}, case=self)   # the state's folder is the PM repo's
        self.pm = os.path.dirname(self.pm_folder)
        p = mock.patch.object(T, 'unignored', lambda pm: None)    # the fake git has no check-ignore
        p.start()
        self.addCleanup(p.stop)
        self.kit = tempfile.mkdtemp(prefix='autotasks-kit-')
        self.addCleanup(gitfixture.rmtree, self.kit)
        touch(os.path.join(self.kit, 'tools', 'worktree_remove.py'))
        touch(os.path.join(self.kit, 'optional', 'agenttemp', 'agent_temp_sweep.py'))
        self.folder = T.state_dir(self.home)
        self.state = os.path.join(self.folder, T.STATE_NAME)
        self.schedules = []
        self.started = []
        self.on_start = None

    def start(self, command, log, cwd):
        if self.on_start:
            self.on_start(command)
        self.started.append((command, log, cwd))
        return 4242

    def seed(self, records):
        os.makedirs(self.folder, exist_ok=True)
        T.write_state(self.state, records)

    def records(self):
        with open(self.state, encoding='utf-8') as f:
            return json.load(f)['tasks']

    def go(self, now=NOW, fetch=None):
        return T.tick(home=self.home, kit=self.kit, now=now, fetch=fetch or (lambda: list(self.schedules)),
                      start=self.start, repos=lambda: [('demo', MAIN, 'demo')], python=PY)


class Table(unittest.TestCase):
    def test_the_fixed_table(self):
        kit = os.path.normpath('C:/kit')
        self.assertEqual(T.argv('maint-lint', kit, MAIN, PY),
                         [PY, os.path.join(kit, 'tools', 'maint_lint.py'), '-C', MAIN])
        self.assertEqual(T.argv('worktree-sweep', kit, MAIN, PY),
                         [PY, os.path.join(kit, 'tools', 'worktree_remove.py'), '-C', MAIN, '--sweep-merged', '--apply'])
        self.assertEqual(T.argv('agent-temp-sweep', kit, None, PY),
                         [PY, os.path.join(kit, 'optional', 'agenttemp', 'agent_temp_sweep.py'), '--apply'])

    def test_unknown_task_names_are_refused(self):
        for name in ('rm', 'Maint-Lint', '', None, '../tools/x', 'worktree-sweep --force'):
            with self.assertRaises(T.UnknownTask):
                T.argv(name, 'C:\\kit', MAIN, PY)

    def test_a_per_repo_task_needs_a_repository(self):
        with self.assertRaises(ValueError):
            T.argv('worktree-sweep', 'C:\\kit', None, PY)

    def test_find_repo_by_name_or_label_any_case_and_never_two(self):
        repos = [('Demo-Label', MAIN, 'demo'), ('other', 'C:\\r\\o', 'o')]
        self.assertEqual(T.find_repo('DEMO', repos), MAIN)
        self.assertEqual(T.find_repo('demo-label', repos), MAIN)
        self.assertIsNone(T.find_repo('nope', repos))
        self.assertIsNone(T.find_repo('x', [('x', 'C:\\a', 'a'), ('y', 'C:\\b', 'x')]))
        self.assertEqual(T.find_repo('x', [('x', 'C:\\a', 'x'), ('y', 'C:\\a', 'x')]), 'C:\\a')

    def test_find_repo_by_checkout_folder_name(self):
        main = os.path.normpath('C:/r/folder-x')
        repos = [('label', main, 'cfgname')]
        self.assertEqual(T.find_repo('Folder-X', repos), main)
        self.assertIsNone(T.find_repo('other', repos))


class State(Base):
    def test_missing_state_is_empty(self):
        self.assertEqual(T.read_state(self.state), ({}, None))

    def test_round_trip_and_no_temp_file_left(self):
        self.seed({'a': {'at': '2026-10-21T09:00:00', 'what': 'started'}})
        self.assertEqual(T.read_state(self.state), ({'a': {'at': '2026-10-21T09:00:00', 'what': 'started'}}, None))
        self.assertEqual(os.listdir(self.folder), [T.STATE_NAME])

    def test_a_broken_state_file_is_empty_with_a_problem(self):
        os.makedirs(self.folder)
        with open(self.state, 'w') as f:
            f.write('{not json')
        records, problem = T.read_state(self.state)
        self.assertEqual(records, {})
        self.assertIn('could not be read', problem)

    def test_the_state_is_in_the_pm_repositorys_machine_config_folder(self):
        from kitlib.config import user as kuser
        self.assertEqual(kuser.locate(self.home).state, kuser.OK)
        self.assertEqual(T.state_dir(self.home), os.path.join(self.pm, kuser.DIR_NAME, T.DIR_NAME))
        self.assertNotEqual(os.path.normcase(T.state_dir(self.home)),
                            os.path.normcase(T.home_state_dir(self.home)))


class Place(Base):
    """Where the state lives: the PM repository's git-ignored machine config folder, never the
    home folder."""

    def home_state(self):
        return os.path.join(self.home, '.pm-kit', 'auto-tasks')

    def test_a_tick_writes_its_state_in_the_pm_repository_and_nothing_in_the_home_folder(self):
        self.schedules = [sched()]
        self.assertEqual(self.go()[0], T.EXIT_OK)
        self.assertTrue(os.path.isfile(os.path.join(self.pm_folder, 'auto-tasks', T.STATE_NAME)))
        self.assertFalse(os.path.lexists(os.path.join(self.home, '.pm-kit')))

    def test_no_pointer_starts_nothing_and_writes_nothing(self):
        from kitlib.config import ConfigError
        with mock.patch.dict(os.environ, {'KIT_PM_REPO_FENCE': ''}):
            from kitlib.config import user as kuser
            kuser.forget()
            self.addCleanup(kuser.forget)
            with self.assertRaises(ConfigError):
                T.state_dir(self.home)
            self.schedules = [sched()]
            code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_FAILED, []))
        self.assertIn('no PM repo pointer', lines[0])
        self.assertIn('nothing started', lines[0])
        self.assertFalse(os.path.lexists(os.path.join(self.home, '.pm-kit')))

    def test_a_state_left_in_the_home_folder_is_refused_with_its_move_and_never_read(self):
        old = self.home_state()
        os.makedirs(old)
        with open(os.path.join(old, T.STATE_NAME), 'w', encoding='utf-8') as fh:
            fh.write('{"version": 1, "tasks": {}}')
        self.schedules = [sched()]
        code, lines = self.go()
        self.assertEqual((code, self.started, len(lines)), (T.EXIT_FAILED, [], 1))
        new = os.path.join(self.pm_folder, 'auto-tasks')
        self.assertIn("Copy-Item -LiteralPath '%s' -Destination '%s' -Recurse" % (old, new), lines[0])
        self.assertIn("Remove-Item -LiteralPath '%s' -Recurse" % old, lines[0])
        self.assertIn('nothing started', lines[0])
        self.assertFalse(os.path.lexists(new))
        self.assertEqual(os.listdir(old), [T.STATE_NAME])

    def test_a_state_in_both_places_names_the_home_ones_removal(self):
        os.makedirs(self.home_state())
        os.makedirs(os.path.join(self.pm_folder, 'auto-tasks'))
        code, lines = self.go()
        self.assertEqual(code, T.EXIT_FAILED)
        self.assertNotIn('Copy-Item', lines[0])
        self.assertIn("Remove-Item -LiteralPath '%s' -Recurse" % self.home_state(), lines[0])

    def test_a_state_folder_git_does_not_ignore_is_refused_and_never_made(self):
        with mock.patch.object(T, 'unignored', lambda pm: 'not ignored, add the line'):
            code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_FAILED, []))
        self.assertEqual(lines, ['not ignored, add the line: nothing started'])
        self.assertFalse(os.path.lexists(os.path.join(self.pm_folder, 'auto-tasks')))

    def test_an_unignored_folder_is_named_before_the_home_states_move(self):
        # moving the home state into a folder git does not ignore leaves it untracked in the checkout
        os.makedirs(self.home_state())
        with mock.patch.object(T, 'unignored', lambda pm: 'not ignored, add the line'):
            code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_FAILED, []))
        self.assertEqual(lines, ['not ignored, add the line: nothing started'])

    def test_with_no_machine_config_folder_yet_the_home_state_says_to_make_it_not_to_copy(self):
        os.makedirs(self.home_state())
        gitfixture.rmtree(self.pm_folder)
        with mock.patch.object(T, 'unignored', lambda pm: 'not ignored, add the line'):
            code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_FAILED, []))
        self.assertEqual(lines, [T.HOME_NO_FOLDER % (self.pm_folder, self.home_state(), T.kuser.MOVE_DOC)
                                 + ': nothing started'])
        self.assertNotIn('Copy-Item', lines[0])
        self.assertIn('config_move.py', lines[0])
        self.assertFalse(os.path.lexists(self.pm_folder))

    def test_git_that_cannot_tell_is_a_problem_saying_why(self):
        from kitlib.config import move
        with mock.patch.object(move, 'ignored', lambda pm, rels: (None, 'fatal: no repo')):
            why = UNIGNORED(self.pm)
        self.assertIn('git could not tell', why)
        self.assertIn('fatal: no repo', why)

    def test_git_that_cannot_run_is_a_problem_saying_why_never_a_raise(self):
        from kitlib.config import move

        def boom(pm, rels):
            raise OSError('git.exe not found')
        with mock.patch.object(move, 'ignored', boom):
            why = UNIGNORED(self.pm)
        self.assertIn('git could not tell', why)
        self.assertIn('OSError: git.exe not found', why)


@real_git('git check-ignore on the PM repo: the fake lacks check-ignore')
class Ignored(unittest.TestCase):
    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.folder = os.path.join(self.repo.root, '.pm-kit')
        os.makedirs(self.folder)

    def ignore(self, text):
        with open(os.path.join(self.folder, '.gitignore'), 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)

    def test_the_moves_ignore_file_ignores_the_state_folder(self):
        from kitlib.config import move
        self.ignore(move.GITIGNORE)
        self.assertIsNone(T.unignored(self.repo.root))

    def test_an_ignore_file_without_the_line_is_a_problem_naming_it(self):
        self.ignore('local.json\n')
        why = T.unignored(self.repo.root)
        self.assertIn('auto-tasks/', why)
        self.assertIn(os.path.join(self.folder, '.gitignore'), why)


class Tick(Base):
    def test_first_sight_records_a_baseline_and_starts_nothing(self):
        self.schedules = [sched()]
        code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_OK, []))
        self.assertEqual(self.records()['worktree-sweep-demo'], {'at': '2026-10-21T12:00:00', 'what': 'seen'})

    def test_due_starts_from_the_table_and_records(self):
        self.schedules = [sched(command='calc.exe', argv=['calc.exe'])]   # extra fields are ignored
        self.seed({'worktree-sweep-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        code, lines = self.go()
        self.assertEqual(code, T.EXIT_OK, lines)
        want = [PY, os.path.join(self.kit, 'tools', 'worktree_remove.py'), '-C', MAIN, '--sweep-merged', '--apply']
        self.assertEqual(len(self.started), 1)
        command, log, cwd = self.started[0]
        self.assertEqual((command, cwd), (want, self.kit))
        self.assertEqual(log, os.path.join(self.folder, 'logs', 'worktree-sweep-demo.log'))
        rec = self.records()['worktree-sweep-demo']
        self.assertEqual((rec['what'], rec['at'], rec['slot'], rec['pid']),
                         ('started', '2026-10-21T12:00:00', '2026-10-21T09:00:00', 4242))
        self.assertIn('worktree-sweep-demo started (pid 4242)', '\n'.join(lines))
        with open(os.path.join(self.folder, T.LOG_NAME), encoding='utf-8') as f:
            self.assertIn('worktree-sweep-demo started', f.read())

    def test_off_and_not_due_start_nothing(self):
        self.schedules = [sched(cadence='off'), sched('agent-temp-sweep', at='13:00')]
        self.seed({'agent-temp-sweep-demo': {'at': '2026-10-20T13:01:00', 'what': 'started'}})
        self.assertEqual(self.go(), (T.EXIT_OK, []))
        self.assertEqual(self.started, [])

    def test_unknown_task_is_refused_never_run(self):
        self.schedules = [sched('format-disk')]
        self.seed({'format-disk-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        code, lines = self.go()
        self.assertEqual(self.started, [])
        self.assertIn('format-disk-demo refused: unknown task', '\n'.join(lines))
        self.assertEqual(self.records()['format-disk-demo']['what'], 'skipped')

    def test_a_tool_not_in_this_kit_is_skipped_once_per_slot(self):
        self.schedules = [sched('maint-lint')]           # tools/maint_lint.py is not in the kit
        self.seed({'maint-lint-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_OK, []))
        self.assertIn('maint-lint-demo skipped: no %s' % os.path.join(self.kit, 'tools', 'maint_lint.py'),
                      '\n'.join(lines))
        self.assertEqual(self.go(NOW + datetime.timedelta(minutes=15)), (T.EXIT_OK, []))

    def test_maint_lint_starts_once_its_tool_is_there(self):
        touch(os.path.join(self.kit, 'tools', 'maint_lint.py'))
        self.schedules = [sched('maint-lint')]
        self.seed({'maint-lint-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        self.go()
        self.assertEqual(self.started[0][0], [PY, os.path.join(self.kit, 'tools', 'maint_lint.py'), '-C', MAIN])

    def test_a_repository_not_in_launcher_repos_is_skipped(self):
        self.schedules = [sched(repo='elsewhere')]
        self.seed({'worktree-sweep-elsewhere': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        code, lines = self.go()
        self.assertEqual(self.started, [])
        self.assertIn("worktree-sweep-elsewhere skipped: repository 'elsewhere'", '\n'.join(lines))

    def test_a_schedule_naming_the_checkout_folder_starts_it(self):
        main = os.path.normpath('C:/r/folder-x')
        self.schedules = [sched(repo='folder-x')]
        self.seed({'worktree-sweep-folder-x': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        T.tick(home=self.home, kit=self.kit, now=NOW, fetch=lambda: self.schedules, start=self.start,
               repos=lambda: [('label', main, 'cfgname')], python=PY)
        self.assertEqual(self.started[0][0][2:4], ['-C', main])

    def test_no_console_python_is_refused_before_the_start_record(self):
        self.schedules = [sched()]
        self.seed({'worktree-sweep-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        with mock.patch.object(T.spawn, 'console_python', return_value=None):
            code, lines = T.tick(home=self.home, kit=self.kit, now=NOW, fetch=lambda: self.schedules,
                                 start=self.start, repos=lambda: [('demo', MAIN, 'demo')])
        self.assertEqual((code, self.started), (T.EXIT_OK, []))
        self.assertIn('worktree-sweep-demo skipped: no python.exe', '\n'.join(lines))
        self.assertEqual(self.records()['worktree-sweep-demo']['what'], 'skipped')

    def test_a_machine_wide_task_starts_once_a_tick(self):
        self.schedules = [sched('agent-temp-sweep', 'a'), sched('agent-temp-sweep', 'b')]
        self.seed({'agent-temp-sweep-a': {'at': '2026-10-20T09:01:00', 'what': 'started'},
                   'agent-temp-sweep-b': {'at': '2026-10-20T09:01:00', 'what': 'started'}})
        self.go()
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.records()['agent-temp-sweep-b']['what'], 'skipped')

    def test_a_failed_start_keeps_its_record_and_fails_the_tick(self):
        self.schedules = [sched()]
        self.seed({'worktree-sweep-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})

        def boom(command, log, cwd):
            raise OSError('no such program')
        code, lines = T.tick(home=self.home, kit=self.kit, now=NOW, fetch=lambda: self.schedules, start=boom,
                             repos=lambda: [('demo', MAIN, 'demo')], python=PY)
        self.assertEqual(code, T.EXIT_FAILED)
        self.assertEqual(self.records()['worktree-sweep-demo']['what'], 'failed')
        self.assertEqual(self.go(NOW + datetime.timedelta(minutes=15)), (T.EXIT_OK, []))


class HubDown(Base):
    def test_nothing_listening_starts_nothing_and_exits_clean_with_one_line(self):
        def down():
            raise serve.NotRunning(1)
        code, lines = self.go(fetch=down)
        self.assertEqual(code, T.EXIT_OK)
        self.assertEqual(len(lines), 1)
        self.assertIn('hub not reachable', lines[0])
        self.assertEqual(self.started, [])
        self.assertFalse(os.path.exists(self.folder))

    def test_the_real_client_on_a_closed_port(self):
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        tokens = tempfile.mkdtemp(prefix='autotasks-tokens-')
        self.addCleanup(gitfixture.rmtree, tokens)
        from kitlib.hubsite import auth, client
        with mock.patch.object(auth, 'read_token', return_value='t'):
            fetch = lambda: client.Client(port, tokens=tokens, timeout=5).listing('schedules')   # noqa: E731
            code, lines = self.go(fetch=fetch)
        self.assertEqual((code, len(lines), self.started), (T.EXIT_OK, 1, []))
        self.assertIn('hub not reachable', lines[0])

    def test_a_refusal_is_a_failure(self):
        from kitlib.hubsite import client

        def refused():
            raise client.Refused(403, {'ok': False, 'error': 'no'})
        code, lines = self.go(fetch=refused)
        self.assertEqual((code, self.started), (T.EXIT_FAILED, []))


class DoubleStart(Base):
    def setUp(self):
        Base.setUp(self)
        self.schedules = [sched()]
        self.seed({'worktree-sweep-demo': {'at': '2026-10-20T09:01:00', 'what': 'started'}})

    def test_a_held_lock_starts_nothing(self):
        lock = TreeLock(os.path.join(self.folder, T.LOCK_NAME), 'the other tick')
        lock.acquire()
        self.addCleanup(lock.release)
        code, lines = self.go()
        self.assertEqual((code, self.started), (T.EXIT_OK, []))
        self.assertIn('another tick holds', lines[0])
        self.assertEqual(self.records()['worktree-sweep-demo']['at'], '2026-10-20T09:01:00')

    def test_the_start_is_on_record_before_the_program_starts(self):
        seen = []
        self.on_start = lambda command: seen.append(self.records()['worktree-sweep-demo'])
        self.go()
        self.assertEqual(seen[0]['what'], 'started')
        self.assertEqual(seen[0]['at'], '2026-10-21T12:00:00')

    def test_the_lock_is_held_while_the_program_starts(self):
        busy = []

        def probe(command):
            other = TreeLock(os.path.join(self.folder, T.LOCK_NAME))
            try:
                other.acquire()
            except T.TreeBusy:
                busy.append(True)
            else:
                other.release()
        self.on_start = probe
        self.go()
        self.assertEqual(busy, [True])

    def test_a_second_tick_after_the_first_starts_nothing(self):
        self.go()
        self.go(NOW + datetime.timedelta(seconds=1))
        self.assertEqual(len(self.started), 1)


class Launch(unittest.TestCase):
    @staticmethod
    def wait_for_exit(pid, seconds=60):
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        k32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
        k32.CloseHandle.argtypes = (ctypes.c_void_p,)
        handle = k32.OpenProcess(0x00100000, False, pid)    # SYNCHRONIZE; None: it already exited
        if handle:
            k32.WaitForSingleObject(handle, seconds * 1000)
            k32.CloseHandle(handle)

    def test_a_real_start_writes_its_output_to_the_log(self):
        import sys
        import time
        folder = tempfile.mkdtemp(prefix='autotasks-launch-')
        self.addCleanup(gitfixture.rmtree, folder)
        log = os.path.join(folder, 'x.log')
        import warnings
        with warnings.catch_warnings():         # the detached child outlives its Popen object
            warnings.simplefilter('ignore', ResourceWarning)
            pid = T.launch([sys.executable, '-c', 'import os; print("ran in", os.getcwd())'], log, folder)
        self.assertIsInstance(pid, int)
        self.addCleanup(self.wait_for_exit, pid)    # runs before the rmtree: the child holds x.log until it exits
        deadline = time.monotonic() + 60
        text = ''
        while time.monotonic() < deadline:
            with open(log, encoding='utf-8') as f:
                text = f.read()
            if 'ran in' in text:
                break
            time.sleep(0.1)
        self.assertIn('ran in %s' % folder, text)


class Tool(unittest.TestCase):
    def test_usage(self):
        from tools import auto_tasks
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(auto_tasks.main([]), auto_tasks.USAGE)
            self.assertEqual(auto_tasks.main(['tick', '--x']), auto_tasks.USAGE)
        self.assertIn('usage: auto_tasks.py tick', err.getvalue())

    def test_tick_prints_its_lines_and_returns_its_code(self):
        from tools import auto_tasks
        out = io.StringIO()
        with mock.patch.object(T, 'tick', return_value=(0, ['hub not reachable (x): nothing started'])), \
                contextlib.redirect_stdout(out):
            self.assertEqual(auto_tasks.main(['tick']), 0)
        self.assertEqual(out.getvalue(), 'hub not reachable (x): nothing started\n')


if __name__ == '__main__':
    unittest.main()
