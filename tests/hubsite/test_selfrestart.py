"""kitlib.hubsite.selfrestart: a task-run hub server's restart on new code. Every Watcher here
runs its work in the caller's thread (run_now, or a run that holds the work back) on a fake
clock, with stand-ins for whether it may restart itself, for the self-check and for the start of
the restarter: no PowerShell, no child python, no scheduled task, no machine config. The
self-check's own work (self_check) runs in this process on a temp store and temp token folder,
port 0, with no sync, tunnel, push, backlog or jobs."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.hubsite import autostart, selfrestart, serve
from kitlib.hubsite import settings as hub_settings
from kitlib.proc import spawn

PORT = 8765
STARTED = (('hubsite', 'serve.py', 1, 10),)
NEW = (('hubsite', 'serve.py', 2, 12),)
NEWER = (('hubsite', 'serve.py', 3, 14),)
PID = 4242


class Clock(object):
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Case(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-selfrestart-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tokens = os.path.join(self.root, 'tokens')
        os.makedirs(self.tokens)
        self.store = os.path.join(self.root, 'store')
        self.clock = Clock()
        self.lines = []
        self.quiet = True
        self.why_not = None             # what the eligible stand-in answers
        self.asked = 0
        self.checked = []               # the stamps on disk when each check ran
        self.check_says = (True, None)
        self.launched = []
        self.launch_says = (PID, None)

    def say(self, text, level='error'):
        self.lines.append((level, text))

    def eligible(self, port, store):
        self.asked += 1
        self.assertEqual((port, store), (PORT, self.store))
        if isinstance(self.why_not, Exception):
            raise self.why_not
        return self.why_not

    def check(self):
        self.checked.append(self.w._seen)
        if isinstance(self.check_says, Exception):
            raise self.check_says
        return self.check_says

    def launch(self, argv):
        self.launched.append(argv)
        if isinstance(self.launch_says, Exception):
            raise self.launch_says
        return self.launch_says

    def watcher(self, tokens='default', run=None):
        self.w = selfrestart.Watcher(
            STARTED, PORT, self.store, self.tokens if tokens == 'default' else tokens, self.say,
            lambda: self.quiet, eligible=self.eligible, check=self.check, launch=self.launch,
            run=run or selfrestart.run_now, clock=self.clock)
        return self.w

    def at(self, dt, stamp):
        """look(stamp) `dt` seconds after the last look."""
        self.clock.t += dt
        self.w.look(stamp)

    def texts(self, level=None):
        return [t for lv, t in self.lines if level is None or lv == level]

    def settle(self, stamp=NEW):
        """A watcher that saw `stamp` and has passed its check for it (no restart yet)."""
        self.quiet = False
        self.watcher()
        self.at(0, stamp)
        self.at(selfrestart.SETTLE_S, stamp)
        self.assertEqual(self.checked, [stamp])
        self.quiet = True


class RestartCase(Case):
    def test_a_settled_checked_quiet_change_starts_the_restarter_once_and_says_so(self):
        w = self.watcher()
        self.at(0, STARTED)
        self.assertEqual((self.lines, self.asked), ([], 0), 'the start stamp is no change')
        self.at(30, NEW)
        self.assertEqual(self.lines, [('note', selfrestart.CHANGED)])
        self.assertEqual(self.asked, 1, 'asked at once whether it may restart itself')
        self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual(self.checked, [NEW])
        self.assertEqual(self.launched, [], 'the check and the restart are separate turns')
        self.at(30, NEW)
        log = os.path.join(self.tokens, selfrestart.LOG_NAME)
        self.assertEqual(self.launched, [selfrestart.restarter_argv(log, PORT)])
        self.assertTrue(w.launched)
        self.assertFalse(w.stale, 'no warning for a restart under way')
        old, new = selfrestart.digest(STARTED), selfrestart.digest(NEW)
        said = self.lines[-1]
        self.assertEqual(said, ('note', selfrestart.LAUNCHED % (old, new, PID, PORT, log)))
        for part in (old, new, 'pid %d' % PID, log):
            self.assertIn(part, said[1])
        with open(log, encoding='utf-8') as f:
            head = f.read()
        self.assertIn('port %d restarting itself: code %s -> %s' % (PORT, old, new), head)
        n = len(self.lines)
        for stamp in (NEW, NEWER, NEWER):
            self.at(selfrestart.SETTLE_S, stamp)
        self.assertEqual((len(self.launched), len(self.checked), len(self.lines)), (1, 1, n),
                         'nothing more once a restarter was started')

    def test_the_log_is_overwritten_for_each_attempt(self):
        log = os.path.join(self.tokens, selfrestart.LOG_NAME)
        with open(log, 'w', encoding='utf-8') as f:
            f.write('an older attempt\n')
        self.settle()
        self.at(1, NEW)
        with open(log, encoding='utf-8') as f:
            self.assertNotIn('an older attempt', f.read())

    def test_no_check_before_the_code_has_stayed_the_same_for_the_settle_time(self):
        self.watcher()
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S - 1, NEW)
        self.assertEqual((self.checked, self.launched), ([], []))
        self.at(1, NEW)
        self.assertEqual(self.checked, [NEW])

    def test_code_that_changes_again_during_the_settle_time_starts_the_wait_again(self):
        self.watcher()
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S - 10, NEWER)
        self.at(20, NEWER)
        self.assertEqual(self.checked, [], 'NEWER has not settled')
        self.at(selfrestart.SETTLE_S, NEWER)
        self.assertEqual(self.checked, [NEWER])
        self.assertEqual(self.texts().count(selfrestart.CHANGED), 1, 'CHANGED said once')

    def test_code_that_changes_after_its_check_passed_is_settled_and_checked_again(self):
        self.settle(NEW)
        self.at(1, NEWER)
        self.assertEqual(self.launched, [], 'the check passed for NEW, not NEWER')
        self.at(selfrestart.SETTLE_S, NEWER)
        self.assertEqual(self.checked, [NEW, NEWER])
        self.assertEqual(self.launched, [])
        self.at(1, NEWER)
        self.assertEqual(len(self.launched), 1)
        self.assertIn(selfrestart.digest(NEWER), self.lines[-1][1])

    def test_code_put_back_to_the_start_stamp_restarts_nothing(self):
        self.watcher()
        self.at(0, NEW)
        self.at(10, STARTED)
        for _ in range(3):
            self.at(selfrestart.SETTLE_S, STARTED)
        self.assertEqual((self.checked, self.launched, self.w.stale), ([], [], False))

    def test_a_request_in_flight_holds_the_restart_until_none_is(self):
        self.settle()
        self.quiet = False
        for _ in range(5):
            self.at(10, NEW)
        self.assertEqual(self.launched, [])
        self.quiet = True
        self.at(10, NEW)
        self.assertEqual(len(self.launched), 1)

    def test_never_quiet_past_the_give_up_time_shows_the_warning_and_still_restarts_later(self):
        self.settle()
        self.quiet = False
        self.at(selfrestart.GIVE_UP_S, NEW)
        self.assertTrue(self.w.stale)
        self.assertEqual(self.lines[-1][0], 'note')
        self.assertIn('has not restarted itself yet', self.lines[-1][1])
        n = len(self.lines)
        self.at(60, NEW)
        self.assertEqual(len(self.lines), n, 'STILL_TRYING said once')
        self.quiet = True
        self.at(60, NEW)
        self.assertEqual(len(self.launched), 1)

    def test_its_work_runs_on_its_own_thread_and_look_does_not_wait_for_it(self):
        held = []
        self.watcher(run=held.append)
        self.at(0, NEW)
        self.assertEqual((len(held), self.asked), (1, 0), 'the probe is handed off, not run')
        self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual(len(held), 1, 'no new work while the last has not ended')
        held.pop()()
        self.at(1, NEW)
        self.assertEqual((self.asked, len(held)), (1, 1), 'collected, then the check handed off')
        self.assertEqual(self.checked, [])
        held.pop()()
        self.assertEqual((self.checked, self.launched), ([NEW], []))
        self.at(1, NEW)
        self.assertEqual(len(self.launched), 1, 'collected, then started')

    def test_a_restarter_that_has_not_restarted_the_server_in_time_shows_the_warning(self):
        self.settle()
        self.at(1, NEW)
        self.at(selfrestart.LAUNCH_WAIT_S - 1, NEW)
        self.assertFalse(self.w.stale)
        self.at(1, NEW)
        self.assertTrue(self.w.stale)
        log = os.path.join(self.tokens, selfrestart.LOG_NAME)
        self.assertEqual(self.lines[-1], ('error', selfrestart.NOT_RESTARTED
                                          % (selfrestart.LAUNCH_WAIT_S, log)))
        n = len(self.lines)
        self.at(100, NEWER)
        self.assertEqual((len(self.lines), len(self.launched)), (n, 1))


class NotHereCase(Case):
    def assert_warn_only(self, why):
        self.watcher()
        self.at(0, NEW)
        self.assertTrue(self.w.stale, 'the warning shows at once')
        self.assertEqual(self.lines, [('note', selfrestart.CHANGED),
                                      ('note', selfrestart.NOT_HERE % why)])
        for _ in range(3):
            self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual((self.checked, self.launched, self.asked), ([], [], 1))

    def test_a_server_that_is_not_the_tasks_only_warns(self):
        self.why_not = selfrestart.NO_TASK % autostart.TASK_NAME
        self.assert_warn_only(self.why_not)

    def test_an_eligibility_question_that_raises_only_warns(self):
        self.why_not = RuntimeError('broken')
        self.assert_warn_only('RuntimeError: broken')

    def test_newer_code_asks_again_and_says_the_same_reason_once(self):
        self.why_not = 'no task here'
        self.watcher()
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S, NEWER)
        self.assertEqual(self.asked, 2, 'asked again for newer code')
        self.assertEqual(self.texts().count(selfrestart.NOT_HERE % 'no task here'), 1)
        self.why_not = None
        self.at(1, NEWER + (('hubsite', 'x.py', 1, 1),))
        self.assertEqual(self.asked, 3)
        self.assertTrue(self.w.stale, 'a warning once shown stays shown')

    def test_a_server_that_stops_being_the_tasks_before_its_check_only_warns(self):
        self.watcher()
        self.at(0, NEW)
        self.why_not = 'the task is Ready'
        self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual((self.checked, self.launched), ([], []))
        self.assertTrue(self.w.stale)
        self.assertIn(('note', selfrestart.NOT_HERE % 'the task is Ready'), self.lines)


class FailureCase(Case):
    def test_a_failed_check_keeps_the_old_server_and_is_not_tried_again_for_that_code(self):
        self.check_says = (False, 'exit 1: ImportError: x')
        self.watcher()
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S, NEW)
        self.assertTrue(self.w.stale)
        self.assertEqual(self.lines[-1], ('error', selfrestart.CHECK_FAILED
                                          % 'exit 1: ImportError: x'))
        for _ in range(3):
            self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual((self.checked, self.launched), ([NEW], []), 'no restart loop')
        self.check_says = (True, None)
        self.at(1, NEWER)
        self.at(selfrestart.SETTLE_S, NEWER)
        self.at(1, NEWER)
        self.assertEqual(self.checked, [NEW, NEWER], 'newer code is tried')
        self.assertEqual(len(self.launched), 1)

    def test_a_check_that_raises_is_a_failed_check(self):
        self.check_says = OSError('no python')
        self.watcher()
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual(self.lines[-1], ('error', selfrestart.CHECK_FAILED
                                          % 'OSError: no python'))
        self.at(1, NEW)
        self.assertEqual(self.launched, [])

    def assert_launch_failed(self, why):
        self.assertTrue(self.w.stale)
        self.assertFalse(self.w.launched)
        if why is not None:
            self.assertEqual(self.lines[-1], ('error', selfrestart.LAUNCH_FAILED % why))
        n = len(self.launched)
        for _ in range(3):
            self.at(selfrestart.SETTLE_S, NEW)
        self.assertEqual(len(self.launched), n, 'not tried again for that code')

    def test_a_restarter_that_cannot_be_started_keeps_the_old_server(self):
        self.launch_says = (None, 'pythonw.exe is not a file')
        self.settle()
        self.at(1, NEW)
        self.assert_launch_failed('pythonw.exe is not a file')

    def test_a_start_that_raises_keeps_the_old_server(self):
        self.launch_says = RuntimeError('wmi')
        self.settle()
        self.at(1, NEW)
        self.assert_launch_failed('RuntimeError: wmi')

    def test_a_log_that_cannot_be_written_starts_no_restarter(self):
        missing = os.path.join(self.root, 'gone')
        self.quiet = False
        self.watcher(tokens=missing)
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S, NEW)
        self.quiet = True
        self.at(1, NEW)
        self.assertEqual(self.launched, [])
        level, said = self.lines[-1]
        self.assertEqual(level, 'error')
        self.assertTrue(said.startswith(selfrestart.LAUNCH_FAILED.split('%')[0]))
        self.assertIn('its log %s cannot be written' % os.path.join(missing, selfrestart.LOG_NAME),
                      said)
        self.assert_launch_failed(None)

    def test_a_server_with_no_token_folder_starts_no_restarter(self):
        self.quiet = False
        self.watcher(tokens=None)
        self.at(0, NEW)
        self.at(selfrestart.SETTLE_S, NEW)
        self.quiet = True
        self.at(1, NEW)
        self.assertEqual(self.launched, [])
        self.assertTrue(self.w.stale)
        self.assertIn('its log None cannot be written', self.lines[-1][1])


class EligibleCase(unittest.TestCase):
    STORE = os.path.join(tempfile.gettempdir(), 'kit-selfrestart-store')

    def settings(self, port=PORT, store=None):
        return hub_settings.settings(None)._replace(port=port, store=store or self.STORE)

    def ask(self, state='Running', settings=None, store=None):
        def task_state():
            if isinstance(state, Exception):
                raise state
            return state

        def load():
            if isinstance(settings, Exception):
                raise settings
            return settings or self.settings()
        return selfrestart.eligible(PORT, store or self.STORE, task_state=task_state,
                                    load_settings=load)

    def test_the_tasks_server_may_restart_itself(self):
        self.assertIsNone(self.ask())
        self.assertIsNone(self.ask(store=self.STORE.upper() + os.sep), 'the same folder')

    def test_no_task(self):
        self.assertEqual(self.ask(state=None), selfrestart.NO_TASK % autostart.TASK_NAME)

    def test_a_task_that_is_not_running(self):
        self.assertEqual(self.ask(state='Ready'),
                         selfrestart.NOT_RUNNING % (autostart.TASK_NAME, 'Ready'))

    def test_a_task_state_that_cannot_be_read(self):
        self.assertEqual(self.ask(state=autostart.AutostartError('timed out')),
                         selfrestart.UNREADABLE % (autostart.TASK_NAME, 'timed out'))

    def test_another_port(self):
        self.assertEqual(self.ask(settings=self.settings(port=9999)),
                         selfrestart.OTHER_PORT % (PORT, 9999))

    def test_another_store(self):
        other = os.path.join(tempfile.gettempdir(), 'elsewhere')
        self.assertEqual(self.ask(settings=self.settings(store=other)),
                         selfrestart.OTHER_STORE % (self.STORE, other))

    def test_a_machine_config_that_cannot_be_read(self):
        got = self.ask(settings=ValueError('bad json'))
        self.assertEqual(got, selfrestart.NO_CONFIG % 'ValueError: bad json')

    def test_the_config_is_read_before_the_task_so_a_scratch_hub_asks_no_powershell(self):
        asked = []
        got = selfrestart.eligible(PORT, self.STORE, task_state=lambda: asked.append(1),
                                   load_settings=lambda: self.settings(port=0))
        self.assertEqual((got, asked), (selfrestart.OTHER_PORT % (PORT, 0), []))


class Result(object):
    def __init__(self, returncode, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class CheckCase(unittest.TestCase):
    def run_check(self, answer):
        seen = []

        def fake(argv, **kw):
            seen.append((argv, kw))
            if isinstance(answer, BaseException):
                raise answer
            return answer
        with mock.patch.object(spawn, 'run', fake):
            got = selfrestart.check(timeout=7)
        return got, seen

    def test_a_child_that_exits_0_passes(self):
        got, seen = self.run_check(Result(0))
        self.assertEqual(got, (True, None))
        argv, kw = seen[0]
        self.assertEqual(argv, [sys.executable, '-B', '-c', selfrestart.CHECK_SNIPPET,
                                selfrestart.KIT_ROOT])
        self.assertEqual(kw['timeout'], 7)

    def test_a_child_that_fails_gives_its_last_line(self):
        got, _ = self.run_check(Result(1, stderr='Traceback\n  File x\nImportError: no y\n\n'))
        self.assertEqual(got, (False, 'exit 1: ImportError: no y'))

    def test_a_child_that_fails_silently(self):
        self.assertEqual(self.run_check(Result(2))[0], (False, 'exit 2: no output'))

    def test_a_long_last_line_is_cut(self):
        got, _ = self.run_check(Result(1, stderr='x' * 1000))
        self.assertEqual(len(got[1]), len('exit 1: ') + selfrestart.DETAIL_MAX)

    def test_a_child_that_does_not_end_in_time(self):
        got, _ = self.run_check(subprocess.TimeoutExpired('python', 7))
        self.assertEqual(got, (False, 'the check did not end within 7 s'))

    def test_a_child_that_cannot_be_started(self):
        got, _ = self.run_check(OSError('no such file'))
        self.assertEqual(got, (False, 'the check could not be started: no such file'))

    def test_the_snippets_compile_and_name_what_this_module_has(self):
        for snippet, name in ((selfrestart.CHECK_SNIPPET, 'self_check'),
                              (selfrestart.RESTARTER_SNIPPET, 'restarter')):
            compile(snippet, 'snippet', 'exec')
            self.assertIn('selfrestart.%s(' % name, snippet)
            self.assertTrue(callable(getattr(selfrestart, name)))


class SelfCheckCase(unittest.TestCase):
    def setUp(self):
        self.made = []

    def make_settings(self, tmp):
        return hub_settings.settings(None)._replace(port=0, store=os.path.join(tmp, 'store'))

    def make_sources(self, tmp):
        self.made.append(tmp)
        return serve.Sources(tokens=os.path.join(tmp, 'tokens'),
                             static_dir=os.path.join(tmp, 'web'))

    def run_self_check(self):
        with mock.patch.object(sys, 'path', list(sys.path)):    # hub.py's top level adds to it
            selfrestart.self_check(make_settings=self.make_settings,
                                   make_sources=self.make_sources)

    def test_the_new_code_imports_builds_starts_and_answers_health(self):
        self.run_self_check()
        self.assertEqual(len(self.made), 1)
        self.assertFalse(os.path.exists(self.made[0]), 'its temp folder is removed')

    def test_a_scratch_hub_that_does_not_answer_health_fails_the_check(self):
        with mock.patch.object(serve, 'ask', lambda *a, **k: (503, {'ok': False})):
            with self.assertRaises(selfrestart.SelfCheckError):
                self.run_self_check()
        self.assertFalse(os.path.exists(self.made[0]))

    def test_a_scratch_hub_that_does_not_start_fails_the_check(self):
        with mock.patch.object(serve.Hub, 'start', lambda self: False):
            with self.assertRaises(selfrestart.SelfCheckError):
                self.run_self_check()

    def test_a_tool_that_will_not_run_fails_the_check_before_any_hub_is_built(self):
        import runpy
        ran = []

        def broken(path, run_name=None):
            ran.append((os.path.relpath(path, selfrestart.KIT_ROOT), run_name))
            raise SyntaxError('invalid syntax')
        with mock.patch.object(runpy, 'run_path', broken):
            with self.assertRaises(SyntaxError):
                self.run_self_check()
        self.assertEqual(ran, [(os.path.join('tools', 'hub.py'), 'hub_self_check')])
        self.assertEqual(self.made, [])

    def test_a_module_that_will_not_import_fails_the_check(self):
        import importlib
        real = importlib.import_module
        names = []

        def picky(name, *a):
            names.append(name)
            if name == 'kitlib.config.user':
                raise ImportError('user is broken')
            return real(name, *a)
        with mock.patch.object(importlib, 'import_module', picky):
            with self.assertRaises(ImportError):
                self.run_self_check()
        for name in ('kitlib.hubsite.serve', 'kitlib.hubjobs', 'kitlib.pmdata'):
            self.assertIn(name, names, 'every code folder is imported, %s too' % name)
        self.assertEqual(self.made, [])

    def test_a_job_module_that_will_not_import_does_not_fail_the_check(self):
        import importlib
        real = importlib.import_module
        names = []

        def no_jobs(name, *a):
            names.append(name)
            if name.startswith('kitlib.hubjobs.'):
                raise ImportError('a job half landed')
            return real(name, *a)
        with mock.patch.object(importlib, 'import_module', no_jobs):
            self.run_self_check()       # the new server serves without it, with a note
        self.assertIn('kitlib.hubjobs', names)
        self.assertEqual(len(self.made), 1)


class RestarterCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='kit-selfrestart-log-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.log = os.path.join(self.root, selfrestart.LOG_NAME)
        self.tool = os.path.join(self.root, 'tools', 'hub.py')
        self.calls = []

    def read(self):
        with open(self.log, encoding='utf-8') as f:
            return f.read()

    def test_it_runs_hub_restart_with_its_output_in_the_log(self):
        def call(argv, stdin=None, stdout=None, stderr=None, cwd=None, env=None):
            self.calls.append((argv, cwd))
            stdout.write('the hub server stopped; the task started it\n')
            return 0
        selfrestart.restarter(self.log, PORT, hub_tool=self.tool, call=call)
        self.assertEqual(self.calls, [([sys.executable, '-B', self.tool, 'restart', '--port',
                                        str(PORT), '--timeout',
                                        str(selfrestart.RESTART_TIMEOUT_S)], self.root)])
        text = self.read()
        self.assertIn('restarter pid %d (started by pid %d)' % (os.getpid(), os.getppid()), text)
        self.assertIn('the hub server stopped; the task started it', text)
        self.assertTrue(text.rstrip().endswith('hub.py restart exit 0'))

    def test_a_restart_that_cannot_be_run_is_said_in_the_log(self):
        def call(argv, **kw):
            raise OSError('no python')
        selfrestart.restarter(self.log, PORT, hub_tool=self.tool, call=call)
        self.assertIn('hub.py restart could not be run: OSError: no python', self.read())

    def test_a_log_that_cannot_be_written_still_runs_the_restart(self):
        log = os.path.join(self.root, 'gone', selfrestart.LOG_NAME)

        def call(argv, stdout=None, **kw):
            self.calls.append(stdout)
            return 3
        selfrestart.restarter(log, PORT, hub_tool=self.tool, call=call)
        self.assertEqual(self.calls, [subprocess.DEVNULL])

    def test_its_argv_runs_the_snippet_windowless_through_the_carrier(self):
        from kitlib.lock import detach
        tail = ['-B', '-c', selfrestart.RESTARTER_SNIPPET, selfrestart.KIT_ROOT, self.log,
                str(PORT)]
        with mock.patch.object(selfrestart.platform, 'NAME', 'windows'):
            argv = selfrestart.restarter_argv(self.log, PORT, python=sys.executable)
        pythonw, console = detach.pythons(sys.executable)
        self.assertEqual(argv, [pythonw, os.path.join(selfrestart.KIT_ROOT, 'tools',
                                                      spawn.CARRIER), console] + tail)
        with mock.patch.object(selfrestart.platform, 'NAME', 'linux'):
            self.assertEqual(selfrestart.restarter_argv(self.log, PORT, python='py'),
                             ['py'] + tail)


class LaunchCase(unittest.TestCase):
    def test_a_restarter_that_cannot_start_is_not_started(self):
        from kitlib.lock import detach
        made = []
        with mock.patch.object(selfrestart.platform, 'NAME', 'windows'), \
                mock.patch.object(detach, 'missing', lambda argv: 'pythonw.exe is not a file'), \
                mock.patch.object(detach, 'create', lambda *a: made.append(a)):
            self.assertEqual(selfrestart.launch(['a', 'b', 'c']),
                             (None, 'pythonw.exe is not a file'))
        self.assertEqual(made, [])

    def test_it_starts_detached_from_the_kit_folder(self):
        from kitlib.lock import detach
        made = []

        def create(argv, cwd, env):
            made.append((argv, cwd, env.get('PYTHONIOENCODING')))
            return PID, None
        with mock.patch.object(selfrestart.platform, 'NAME', 'windows'), \
                mock.patch.object(detach, 'missing', lambda argv: None), \
                mock.patch.object(detach, 'create', create):
            self.assertEqual(selfrestart.launch(['a', 'b', 'c']), (PID, None))
        self.assertEqual(made, [(['a', 'b', 'c'], selfrestart.KIT_ROOT, 'utf-8')])


class DigestCase(unittest.TestCase):
    def test_a_digest_names_the_stamp_and_its_file_count(self):
        self.assertEqual(selfrestart.digest(NEW), selfrestart.digest(tuple(NEW)))
        self.assertNotEqual(selfrestart.digest(NEW), selfrestart.digest(NEWER))
        self.assertTrue(selfrestart.digest(NEW).endswith(' (1 files)'))
        self.assertTrue(selfrestart.digest(None).endswith(' (0 files)'))


if __name__ == '__main__':
    unittest.main()
