"""kitlib.hubsite.jobs: the hub's timed jobs (cadence, the failure count and its pushes, the stop),
their health block, the job push (kitlib.hubsite.push PushWorker.job_event) and their wiring in
kitlib.hubsite.serve. The plugin refresh job is in tests/pluginsource/test_hub_job.py."""
import importlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib import hubjobs
from kitlib.hubsite import jobs, push, serve
from tests.home import pm_repo
from tests.hubsite.httpfix import get
from tests.hubsite.test_push import PushCase
from tests.hubsite.test_serve import HEALTH, ServeCase

WAIT_S = 10


class Boom(Exception):
    pass


class Script(object):
    """A job's run: each call takes the next of `steps` (a text to return, an exception to raise);
    past the end it returns None. `calls` counts them; `entered`/`release` hold a run open."""

    def __init__(self, steps=(), hold=False):
        self.steps = list(steps)
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()
        self.ran = threading.Event()

    def __call__(self, ctx):
        self.calls += 1
        self.entered.set()
        self.release.wait(WAIT_S)
        step = self.steps.pop(0) if self.steps else None
        self.ran.set()
        if isinstance(step, Exception):
            raise step
        return step


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.lines = []
        self.t = [100.0]

    def make(self, *job_list):
        js = jobs.Jobs(list(job_list), notify=lambda n, w: self.events.append((n, w)),
                       log=self.lines.append, clock=lambda: self.t[0])
        self.addCleanup(js.stop, WAIT_S)
        return js

    def test_a_job_runs_first_after_first_s_then_every_every_s(self):
        run = Script()
        js = self.make(jobs.Job('tick', 0.05, run, first_s=0.05))
        self.assertEqual(run.calls, 0, 'nothing runs before start')
        js.start()
        deadline = time.monotonic() + WAIT_S
        while run.calls < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(run.calls, 3, 'it runs again on its cadence')
        self.assertTrue(js.stop(WAIT_S))
        seen = run.calls
        time.sleep(0.2)
        self.assertEqual(run.calls, seen, 'no run after the stop')
        self.assertGreaterEqual(js.status()['tick']['runs'], 3)

    def test_the_first_run_waits_for_first_s(self):
        run = Script()
        js = self.make(jobs.Job('slow', 0.01, run, first_s=30))
        js.start()
        time.sleep(0.2)
        self.assertEqual(run.calls, 0)
        self.assertTrue(js.stop(WAIT_S), 'the stop ends a wait for the first run at once')

    def test_failures_count_push_failing_once_at_the_third_and_recovered_after(self):
        run = Script([Boom('one'), Boom('two'), Boom('three'), Boom('four'), 'fine'])
        js = self.make(jobs.Job('j', 300, run))
        js.run_now('j')
        js.run_now('j')
        st = js.status()['j']
        self.assertEqual((st['failures'], st['error'], st['ok_at']), (2, 'Boom: two', None))
        self.assertEqual(self.events, [], 'no push before the FAIL_PUSH-th failure')
        js.run_now('j')
        self.assertEqual(self.events, [('j', 'failing')])
        js.run_now('j')
        self.assertEqual(self.events, [('j', 'failing')], 'failing is pushed once')
        self.assertEqual(js.status()['j']['failures'], 4)
        self.t[0] = 200.0
        js.run_now('j')
        st = js.status()['j']
        self.assertEqual(self.events, [('j', 'failing'), ('j', 'recovered')])
        self.assertEqual((st['failures'], st['error'], st['note'], st['ok_at'], st['runs']),
                         (0, None, 'fine', 200.0, 5))
        self.assertEqual(len(self.lines), 4, 'each failure is a log line')

    def test_a_job_that_recovers_before_failing_was_pushed_pushes_nothing(self):
        run = Script([Boom('x'), 'ok'])
        js = self.make(jobs.Job('j', 300, run))
        js.run_now('j')
        js.run_now('j')
        self.assertEqual(self.events, [])

    def test_a_skip_leaves_the_count_and_the_episode_alone_and_pushes_nothing(self):
        run = Script([Boom('a'), jobs.Skip('busy'), Boom('b'), jobs.Skip('off'), Boom('c'),
                      jobs.Skip('waiting'), 'done'])
        js = self.make(jobs.Job('j', 300, run))
        for _ in range(3):
            js.run_now('j')
        st = js.status()['j']
        self.assertEqual((st['failures'], st['note'], st['ok_at']), (2, 'busy', None),
                         'a skip neither resets nor adds to the count')
        js.run_now('j')
        js.run_now('j')
        self.assertEqual(self.events, [('j', 'failing')], 'three real failures, skips between')
        js.run_now('j')
        st = js.status()['j']
        self.assertEqual((st['failures'], st['error'], st['note']), (3, 'Boom: c', 'waiting'))
        self.assertEqual(self.events, [('j', 'failing')], 'a skip is not a recovery')
        self.t[0] = 300.0
        js.run_now('j')
        self.assertEqual(self.events, [('j', 'failing'), ('j', 'recovered')])
        self.assertEqual((js.status()['j']['ok_at'], js.status()['j']['runs']), (300.0, 7))

    def test_a_running_job_is_not_run_twice_at_once(self):
        run = Script(hold=True)
        js = self.make(jobs.Job('j', 300, run))
        first = threading.Thread(target=js.run_now, args=('j',))
        first.start()
        self.assertTrue(run.entered.wait(WAIT_S))
        self.assertTrue(js.status()['j']['running'])
        self.assertIs(js.run_now('j'), False, 'a second run is refused while one runs')
        run.release.set()
        first.join(WAIT_S)
        self.assertEqual(run.calls, 1)
        self.assertFalse(js.status()['j']['running'])
        self.assertIs(js.run_now('j'), True)

    def test_stop_says_whether_every_thread_ended_in_time(self):
        run = Script(hold=True)
        js = self.make(jobs.Job('j', 300, run, first_s=0))
        js.start()
        self.assertTrue(run.entered.wait(WAIT_S))
        self.assertIs(js.stop(0.05), False, 'a run in flight outlasts the time')
        run.release.set()
        self.assertIs(js.stop(WAIT_S), True)

    def test_a_job_sees_the_stop_and_its_events_reach_notify(self):
        seen = []

        def run(ctx):
            ctx.event('hello')
            seen.append(ctx.stop.is_set())
        js = self.make(jobs.Job('j', 300, run))
        js.run_now('j')
        js.stop(WAIT_S)
        js.run_now('j')
        self.assertEqual(seen, [False, True])
        self.assertEqual(self.events, [('j', 'hello'), ('j', 'hello')])

    def test_an_event_detail_reaches_notify_as_a_third_argument_only_when_given(self):
        got = []

        def run(ctx):
            ctx.event('refreshed', 'abc123')
            ctx.event('plain')
        js = jobs.Jobs([jobs.Job('j', 300, run)], notify=lambda *a: got.append(a))
        js.run_now('j')
        self.assertEqual(got, [('j', 'refreshed', 'abc123'), ('j', 'plain')])

    def test_a_notify_that_raises_is_a_line_and_the_job_goes_on(self):
        def notify(name, word):
            raise RuntimeError('down')
        js = jobs.Jobs([jobs.Job('j', 300, Script([Boom(), Boom(), Boom()]))], notify=notify,
                       log=self.lines.append)
        for _ in range(3):
            js.run_now('j')
        self.assertEqual(js.status()['j']['failures'], 3)
        self.assertTrue(any('not pushed' in line for line in self.lines))

    def test_two_jobs_of_one_name_are_refused_and_an_unknown_name_is_a_keyerror(self):
        with self.assertRaises(ValueError):
            jobs.Jobs([jobs.Job('a', 1, Script()), jobs.Job('a', 1, Script())])
        with self.assertRaises(KeyError):
            jobs.Jobs([]).run_now('nope')

    def test_the_health_block_before_any_run(self):
        js = self.make(jobs.Job('j', 300, Script()))
        self.assertEqual(js.status(), {'j': {'every_s': 300, 'runs': 0, 'failures': 0,
                                             'running': False, 'last_at': None, 'ok_at': None,
                                             'note': None, 'error': None}})


class JobPushTest(PushCase):
    def jobs_sent(self):
        return [t for t in self.push.to('pm') if t.startswith('[hub push] job ')]

    def test_a_job_event_is_pushed_to_the_pm_once_written(self):
        self.worker.job_event('plugin-refresh', 'failing')
        self.worker.send_jobs()
        sent = self.jobs_sent()
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].splitlines()[0],
                         '[hub push] job plugin-refresh failing route:pm attempt:1')
        self.assertIn('jobs.plugin-refresh', sent[0])
        self.t += push.SCHEDULE_S[-1]
        self.worker.send_jobs()
        self.assertEqual(len(self.jobs_sent()), 1, 'written: not sent again')

    def test_a_job_push_not_written_is_tried_on_the_schedule_and_a_newer_event_replaces_it(self):
        self.push.states['pm'] = push.NO_SESSION
        self.worker.job_event('j', 'failing')
        self.worker.send_jobs()
        self.worker.send_jobs()
        self.assertEqual(len(self.jobs_sent()), 1, 'not again before its slot')
        self.t += push.SCHEDULE_S[0]
        self.worker.send_jobs()
        self.assertEqual(len(self.jobs_sent()), 2)
        self.push.states['pm'] = push.WRITTEN
        self.worker.job_event('j', 'recovered')
        self.worker.send_jobs()
        self.assertEqual(self.jobs_sent()[-1].splitlines()[0],
                         '[hub push] job j recovered route:pm attempt:1')
        self.t += push.SCHEDULE_S[-1]
        self.worker.send_jobs()
        self.assertEqual(len(self.jobs_sent()), 3)

    def test_a_detail_is_a_header_field_cut_to_its_alphabet_and_a_newer_event_replaces_it(self):
        self.push.states['pm'] = push.NO_SESSION
        self.worker.job_event('j', 'refreshed', 'abc123 to\ndef')
        self.worker.send_jobs()
        self.assertEqual(self.jobs_sent()[0].splitlines()[0],
                         '[hub push] job j refreshed detail:abc123_to_def route:pm attempt:1')
        self.push.states['pm'] = push.WRITTEN
        self.worker.job_event('j', 'recovered')
        self.worker.job_event('k', 'found', 7)
        self.t += push.SCHEDULE_S[0]
        self.worker.send_jobs()
        heads = sorted(t.splitlines()[0] for t in self.jobs_sent()[1:])
        self.assertEqual(heads, ['[hub push] job j recovered route:pm attempt:1',
                                 '[hub push] job k found detail:7 route:pm attempt:1'],
                         'one owed push per job: the newer event, with no detail, replaced it')
        self.t += push.SCHEDULE_S[-1]
        self.worker.send_jobs()
        self.assertEqual(len(self.jobs_sent()), 3, 'both written: nothing owed')

    def test_a_job_name_is_cut_to_the_header_alphabet(self):
        self.worker.job_event('bad name\nline', 'fail ing')
        self.worker.send_jobs()
        self.assertEqual(self.jobs_sent()[0].splitlines()[0],
                         '[hub push] job bad_name_line fail_ing route:pm attempt:1')


class WiredTest(ServeCase):
    def test_build_runs_the_jobs_start_and_close_and_health_shows_them(self):
        run = Script(['first note'])
        made = []

        def maker(sources):
            made.append(sources)
            return jobs.Job('demo', 300, run, first_s=0)
        hub = self.build(jobs=[maker])
        self.assertEqual(len(made), 1)
        self.assertEqual(hub.jobs.names(), ['demo'])
        self.assertEqual(run.calls, 0, 'build starts nothing')
        hub.start()
        self.assertTrue(run.ran.wait(WAIT_S))
        deadline = time.monotonic() + WAIT_S
        while hub.jobs.status()['demo']['runs'] < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        block = get(hub.port, HEALTH, self.agent).json()['jobs']['demo']
        self.assertEqual((block['runs'], block['note'], block['every_s']), (1, 'first note', 300))
        self.assertTrue(hub.close())
        self.assertFalse(any(t.name == 'hub-job-demo' for t in threading.enumerate()),
                         'the close ended the job thread')

    def test_no_jobs_is_null_in_health_and_a_maker_that_raises_is_a_note(self):
        def broken(sources):
            raise RuntimeError('no')
        hub = self.build(jobs=[broken])
        self.assertIsNone(hub.jobs)
        self.assertTrue(any('a timed job was not made' in n for n in hub.notes))
        hub.start()
        self.assertIsNone(get(hub.port, HEALTH, self.agent).json()['jobs'])

    def test_a_job_that_holds_the_close_is_an_unclean_step(self):
        run = Script(hold=True)
        hub = self.build(jobs=[lambda s: jobs.Job('held', 300, run, first_s=0)])
        hub.start()
        self.assertTrue(run.entered.wait(WAIT_S))
        self.assertFalse(hub.close(0.1))
        self.assertTrue(any('the timed jobs' in e for e in self.errors()))
        run.release.set()



class MachineJobsTest(unittest.TestCase):
    """machine_sources under a temp home (as test_serve MachineCase)."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-hubjobs-')
        self.addCleanup(shutil.rmtree, self.home, True)
        env = mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})
        env.start()
        self.addCleanup(env.stop)

    def test_machine_sources_gives_every_discovered_job_only_with_sync(self):
        from kitlib.hubsite import plugin_job
        pm_repo(self, record=False)
        src = serve.machine_sources(self.home)
        found = [m.module for m in hubjobs.makers()]
        self.assertIn('kitlib.hubjobs.plugin_refresh', found)
        self.assertEqual([m.module for m in src.jobs], found,
                         'the server runs what discovery finds, no list of its own')
        self.assertEqual(src.home, self.home)
        self.assertEqual(serve.machine_sources(self.home, sync=False).jobs, [],
                         'a scratch store runs no job')
        self.assertEqual(serve.Sources().jobs, [])
        maker = src.jobs[found.index('kitlib.hubjobs.plugin_refresh')]
        job = maker(src)
        self.assertEqual((job.name, job.every_s), (plugin_job.NAME, plugin_job.EVERY_S))
        self.assertEqual(job.run.home, self.home)


class FakeJobs(object):
    """A job package made in a temp folder and imported, for discovery: `pkg` the package,
    `name` its import name. Each module is {file name: source}."""

    def __init__(self, case, modules):
        root = tempfile.mkdtemp(prefix='kit-hubjobs-pkg-')
        case.addCleanup(shutil.rmtree, root, True)
        self.name = 'fakehubjobs_%d' % id(self)
        folder = os.path.join(root, self.name)
        os.makedirs(os.path.join(folder, 'subpkg'))
        for rel, text in dict(modules, **{'__init__.py': '', 'subpkg/__init__.py':
                                          'def make(sources):\n    raise AssertionError\n'}).items():
            with open(os.path.join(folder, rel), 'w', encoding='utf-8') as f:
                f.write(text)
        sys.path.insert(0, root)
        case.addCleanup(self._forget, root)
        self.pkg = importlib.import_module(self.name)

    def _forget(self, root):
        sys.path.remove(root)
        for mod in [m for m in sys.modules if m == self.name or m.startswith(self.name + '.')]:
            del sys.modules[mod]


JOB = ('from kitlib.hubsite import jobs\n'
       'def make(sources):\n'
       '    return jobs.Job(%r, 300, lambda ctx: %r, first_s=0)\n')


class DiscoveryTest(ServeCase):
    def test_makers_find_each_module_but_private_ones_and_packages_and_import_nothing(self):
        fake = FakeJobs(self, {'beta.py': JOB % ('beta', 'b'), 'alpha.py': JOB % ('alpha', 'a'),
                               '_shared.py': 'raise AssertionError("a helper is not a job")\n'})
        made = hubjobs.makers(fake.pkg)
        self.assertEqual([m.module for m in made],
                         [fake.name + '.alpha', fake.name + '.beta'])
        self.assertNotIn(fake.name + '.alpha', sys.modules, 'nothing is imported by makers()')
        self.assertEqual(made[0](None).name, 'alpha')

    def test_build_runs_discovered_jobs_and_serves_past_a_broken_none_or_twice_named_one(self):
        fake = FakeJobs(self, {
            'a_ok.py': JOB % ('ok', 'ran'),
            'b_broken.py': 'import no_such_module_anywhere\n',
            'c_none.py': 'def make(sources):\n    return None\n',
            'd_twin.py': JOB % ('ok', 'twin')})
        hub = self.build(jobs=hubjobs.makers(fake.pkg))
        self.assertEqual(hub.jobs.names(), ['ok'])
        broken = [n for n in hub.notes if fake.name + '.b_broken' in n]
        self.assertEqual(len(broken), 1, hub.notes)
        self.assertIn('ModuleNotFoundError', broken[0])
        twin = [n for n in hub.notes if fake.name + '.d_twin' in n]
        self.assertEqual(len(twin), 1, hub.notes)
        self.assertIn("named 'ok'", twin[0])
        self.assertFalse(any('c_none' in n for n in hub.notes), 'None is left out silently')
        hub.jobs.run_now('ok')
        self.assertEqual(hub.jobs.status()['ok']['note'], 'ran', 'the first of the name runs')
        self.assertTrue(hub.close())

    def test_the_package_holds_only_job_modules_each_with_make(self):
        for maker in hubjobs.makers():
            mod = importlib.import_module(maker.module)
            self.assertTrue(callable(getattr(mod, 'make', None)), maker.module)


class CodeStampTest(unittest.TestCase):
    def test_the_jobs_folder_is_hub_code_too(self):
        self.assertIn(os.path.dirname(os.path.abspath(hubjobs.__file__)), serve.code_folders())

    def test_a_job_module_added_changes_the_stamp(self):
        a = tempfile.mkdtemp(prefix='kit-stamp-a-')
        b = tempfile.mkdtemp(prefix='kit-stamp-b-')
        for d in (a, b):
            self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(a, 'x.py'), 'w') as f:
            f.write('x = 1\n')
        before = serve.code_stamp([a, b])
        with open(os.path.join(b, 'new_job.py'), 'w') as f:
            f.write('def make(sources):\n    return None\n')
        self.assertNotEqual(serve.code_stamp([a, b]), before)
        self.assertEqual(serve.code_stamp([a, b]), serve.code_stamp([a, b]))


if __name__ == '__main__':
    unittest.main()
