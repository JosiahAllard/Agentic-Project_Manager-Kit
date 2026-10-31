"""gate.py stop (kitlib.gate.stop): what it refuses, what it can never end, and what a stopped gate
leaves behind. Every lock run here is on the scratch store. A run whose wrapper is this test
process must never reach the forced end, so StopCase replaces it with `self.force` unless the
class is REAL_FORCE, whose wrappers are real child processes."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from kitlib import gate as G
from kitlib.gate import land, rerun, result, runner, steps, stop
from kitlib.lock import holds as hl
from kitlib.lock import run as lockrun
from kitlib.lock.store import LockError, utc_now
from kitlib.proc import process
from kitlib.records import gate_ok, landing
from tests.gate import fixtures
from tests.gate.test_entry import GATE_PY, gate_py
from tests.lock.helpers import OWNER, agent, claude_of

R1 = 'r-20261030T120000Z-a1a1a1'
R2 = 'r-20261030T120000Z-b2b2b2'
TIP = 'a' * 40
WAIT_S = 60
FORCED = G.Outcome(G.OK, ['FORCED'], stopped=False)
HANG = 'import os, time; open(os.environ["STEP_UP"], "w").close(); time.sleep(300)'
AFTER = 'import os; open(os.environ["STEP_AFTER"], "w").close(); print("ok")'
HANGING = {'steps': [
    {'name': 'hang', 'always': True, 'continue': True, 'cmd': ['{python}', '-c', HANG],
     'pass': '^ok$'},
    {'name': 'after', 'always': True, 'cmd': ['{python}', '-c', AFTER], 'pass': '^ok$'}]}


class StopCase(fixtures.GateRepoCase):
    REAL_FORCE = False

    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        if not self.REAL_FORCE:
            patcher = mock.patch.object(stop, '_force', return_value=FORCED)
            self.force = patcher.start()
            self.addCleanup(patcher.stop)

    def gate_run(self, rid, **fields):
        return self.live_record(rid, gate=True, **fields)

    def stop(self, rid, actor=OWNER, root=None):
        return stop.stop_run(rid, self.store, root=root, actor=actor)

    def stop_file(self, rid):
        return self.store.path('active', rid + '.stop')

    def sleeper(self):
        """A live process that is no part of any run: (Popen, its ctime)."""
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        self.addCleanup(p.wait, 30)
        self.addCleanup(p.kill)
        return p, process.proc_ctime(p.pid)

    def dead_process(self):
        p, ctime = self.sleeper()
        p.kill()
        p.wait(30)
        return p.pid, ctime

    def running_record(self, pid, ctime, branch='kit/feature', kind='gate', tip=TIP):
        rec = result.build(branch, tip, None, kind, None, verdict=result.RUNNING, pid=pid,
                           ctime=ctime)
        return result.write(self.repo.root, rec), rec

    def assert_refused(self, out, *words):
        self.assertEqual(out.code, G.REFUSED, out.lines)
        self.assertTrue(out.lines[0].startswith('REFUSED: '), out.lines)
        self.assertIn('nothing was stopped', out.lines[0])
        for word in words:
            self.assertIn(word, out.lines[0])


class NotAGate(StopCase):
    def test_a_bad_id_is_usage(self):
        out = self.stop('../active/x')
        self.assertEqual(out.code, G.USAGE, out.lines)
        self.assertTrue(out.lines[0].startswith('USAGE: '), out.lines)

    def test_a_run_with_no_record_is_refused(self):
        self.assert_refused(self.stop(R1), 'no record of run %s' % R1)

    def test_a_run_that_is_not_a_gate_is_refused_and_keeps_running(self):
        self.live_record(R1)
        self.assert_refused(self.stop(R1), 'not a gate run')
        self.assertFalse(os.path.exists(self.stop_file(R1)))
        self.force.assert_not_called()

    def test_an_ended_run_that_was_no_gate_is_refused(self):
        self.store.write('ended/%s.json' % R1, {'id': R1, 'how': 'done', 'code': 0})
        self.assert_refused(self.stop(R1), 'not a gate run')


class Landings(StopCase):
    def assert_untouched(self, rid, *words):
        self.assert_refused(self.stop(rid, root=self.repo.root), 'never touches a landing', *words)
        self.assertFalse(os.path.exists(self.stop_file(rid)))
        self.force.assert_not_called()

    def test_each_mark_of_a_landing_run_is_refused(self):
        for i, mark in enumerate(({'priority': 'land'}, {'resource': 'somewhere'},
                                  {'note': 'landing: kit/x'})):
            with self.subTest(mark):
                rid = R1[:-1] + str(i)
                self.gate_run(rid, **mark)
                self.assert_untouched(rid)

    def test_an_ended_landing_run_is_refused_and_its_record_stays(self):
        self.store.write('ended/%s.json' % R1, {'id': R1, 'gate': True, 'priority': 'land',
                                                'how': 'done', 'code': 0, 'pid': 1, 'ctime': 1})
        self.assert_untouched(R1)

    def test_a_real_landing_s_gate_is_refused_while_its_lock_is_held(self):
        _, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        fixtures.gate_full(self.repo.root, tip)
        fixtures.queue_review(self.repo.root, 'kit/feature', tip)
        seen = {}

        def held(lander):
            lock = landing.LandingLock(self.repo.root)
            before = lock.read()
            seen['out'] = self.stop(lander.slot.id, root=self.repo.root)
            seen['stop'] = os.path.exists(self.stop_file(lander.slot.id))
            seen['same'] = lock.read() == before
            return runner.merged_gate(lander.root, lander.cfg, lander.expect_main,
                                      log_dir=self.logs, book_kw=self.book_kw, slot=lander.slot)
        out = land.Lander(self.repo.root, 'kit/feature', self.main, tip, git_env=self.env,
                          book_kw=self.book_kw, log_dir=self.logs, merged_gate=held).run()
        self.assert_refused(seen['out'], 'never touches a landing')
        self.assertEqual((seen['stop'], seen['same']), (False, True))
        self.assertEqual(out.code, G.OK, out.lines)     # and the landing went on to land
        self.force.assert_not_called()

    def test_a_gate_under_a_landing_s_process_is_refused(self):
        p, ctime = self.sleeper()
        self.live_record(R2, priority='land', pid=p.pid, ctime=ctime)
        self.gate_run(R1, ancestors=[[p.pid, ctime]])
        self.assert_untouched(R1, 'landing run %s' % R2)

    def test_a_gate_nested_in_a_landing_s_run_is_refused(self):
        p, ctime = self.sleeper()
        self.live_record(R2, resource='somewhere', pid=p.pid, ctime=ctime)
        self.gate_run(R1, parent=R2)
        self.assert_untouched(R1, 'landing run %s' % R2)

    def test_a_gate_under_a_running_land_record_is_refused(self):
        p, ctime = self.sleeper()
        self.running_record(p.pid, ctime, kind='land')
        self.gate_run(R1, ancestors=[[p.pid, ctime]])
        self.assert_untouched(R1, 'the landing of kit/feature')

    def write_landing_lock(self, text):
        path = landing.LandingLock(self.repo.root).path
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def assert_lock_kept(self, path, text):
        with open(path, encoding='utf-8') as f:
            self.assertEqual(f.read(), text)

    def test_the_holder_of_the_landing_lock_is_refused_and_the_lock_stays(self):
        text = json.dumps({'token': 't', 'pid': os.getpid(), 'pid_start': process.own_ctime(),
                           'branches': ['kit/x'], 'phase': 'gate'})
        path = self.write_landing_lock(text)
        self.gate_run(R1)
        self.assert_untouched(R1, 'holds the landing lock')
        self.assert_lock_kept(path, text)

    def test_a_landing_lock_that_cannot_be_read_is_taken_as_held(self):
        path = self.write_landing_lock('not json')
        self.gate_run(R1)
        self.assert_untouched(R1, 'cannot be read')
        self.assert_lock_kept(path, 'not json')

    def test_another_s_landing_lock_does_not_refuse_a_gate(self):
        text = json.dumps({'token': 't', 'pid': 4, 'pid_start': 1, 'branches': ['kit/x'],
                           'phase': 'gate'})
        path = self.write_landing_lock(text)
        self.store.settings.stop_s = 0.05
        self.gate_run(R1)
        self.assertIs(self.stop(R1, root=self.repo.root), FORCED)
        self.assert_lock_kept(path, text)


class Sessions(StopCase):
    def setUp(self):
        StopCase.setUp(self)
        self.store.settings.stop_s = 0.05
        self.gate_run(R1, session='S1', ancestors=[claude_of('S1')])

    def test_another_session_is_refused_and_the_run_goes_on(self):
        self.assert_refused(self.stop(R1, actor=agent('S2')), 'another session')
        self.assertFalse(os.path.exists(self.stop_file(R1)))
        self.force.assert_not_called()

    def test_the_session_it_belongs_to_may_stop_it(self):
        self.assertIs(self.stop(R1, actor=agent('S1')), FORCED)
        self.assertTrue(os.path.exists(self.stop_file(R1)))

    def test_the_owner_may_stop_any_session_s_run(self):
        self.assertIs(self.stop(R1), FORCED)
        self.assertTrue(os.path.exists(self.stop_file(R1)))


class Gone(StopCase):
    def test_a_run_already_ended_is_said_and_its_record_stands(self):
        pid, ctime = self.dead_process()
        path, rec = self.running_record(pid, ctime)
        passed = dict(rec, verdict='pass', code=0)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(passed, f)
        self.store.write('ended/%s.json' % R1, {'id': R1, 'gate': True, 'repo': 'kit', 'slug': 't',
                                                'how': 'done', 'code': 0, 'pid': pid,
                                                'ctime': ctime})
        out = self.stop(R1)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(out.stopped)
        self.assertTrue(out.lines[0].startswith('ALREADY GONE: gate run %s' % R1), out.lines)
        self.assertIn('had ended (done, code 0); nothing was stopped', out.lines[0])
        self.assertEqual(out.lines[1], 'result record: pass: %s' % path)
        self.assertEqual(result.read_path(path)[0]['verdict'], 'pass')

    def test_an_ended_run_s_leftover_running_record_is_finished(self):
        pid, ctime = self.dead_process()
        path, _ = self.running_record(pid, ctime)
        self.store.write('ended/%s.json' % R1, {'id': R1, 'gate': True, 'how': 'reaped',
                                                'code': None, 'pid': pid, 'ctime': ctime})
        out = self.stop(R1)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertEqual(result.read_path(path)[0]['verdict'], result.ERROR)
        self.assertEqual(out.lines[1], 'result record: error: %s' % path)

    def test_a_run_whose_process_died_is_moved_by_the_lock_s_own_reap(self):
        pid, ctime = self.dead_process()
        path, _ = self.running_record(pid, ctime)
        self.gate_run(R1, pid=pid, ctime=ctime)
        out = self.stop(R1)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(out.stopped)
        self.assertIn('its process had died; nothing was stopped', out.lines[0])
        self.assertEqual(out.lines[1], 'lock: the run is ended (reaped) and its slots are free')
        self.assertIsNone(self.store.read('active/%s.json' % R1))
        self.assertEqual(self.store.read('ended/%s.json' % R1)['how'], 'reaped')
        self.assertEqual(result.read_path(path)[0]['verdict'], result.ERROR)    # not `stopped`
        self.force.assert_not_called()

    def test_a_run_with_no_result_record_says_so(self):
        pid, ctime = self.dead_process()
        self.gate_run(R1, pid=pid, ctime=ctime)
        self.assertIn('result record: none names this run', self.stop(R1).lines)

    def test_only_a_stop_s_own_end_is_read_as_a_stop(self):
        asked = {'stopped_by': {'by': 'gate.py stop'}}
        for ended, stopped in (({'how': 'cancelled', 'code': 1}, True),
                               (dict(asked, how='not acquired', code=1), True),
                               ({'how': 'not acquired', 'code': 1}, False),
                               (dict(asked, how='not acquired', code=8), False),
                               (dict(asked, how='done', code=0), False)):
            with self.subTest(ended):
                self.assertEqual(stop._by_stop(ended), stopped)

    def test_a_run_that_ended_by_itself_is_not_called_stopped(self):
        self.gate_run(R1, session='S1')

        def ends_first(actor, rid, store):
            lockrun_record = store.read('active/%s.json' % rid)
            store.write('ended/%s.json' % rid, dict(lockrun_record, how='done', code=0))
            os.remove(store.path('active', rid + '.json'))
            return {'ok': False, 'error': {'code': 'not_found', 'message': 'gone'}}
        with mock.patch.object(stop.api, 'end_run', ends_first):
            out = self.stop(R1)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(out.stopped)
        self.assertIn('ended by itself (done, code 0) before the stop reached it', out.lines[0])


class NeverAnotherProcess(StopCase):
    REAL_FORCE = True

    def setUp(self):
        StopCase.setUp(self)
        self.store.settings.stop_s = 0.05

    def test_a_recycled_pid_is_never_ended(self):
        p, ctime = self.sleeper()
        self.gate_run(R1, pid=p.pid, ctime=ctime + 1)
        out = self.stop(R1)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertFalse(out.stopped)
        self.assertIsNone(p.poll())
        self.assertTrue(process.alive(p.pid, ctime))

    def test_a_pid_recycled_after_the_stop_was_asked_is_never_ended(self):
        p, ctime = self.sleeper()
        self.gate_run(R1, pid=p.pid, ctime=ctime + 1)
        real_live, real_end = self.store.run_live, process.terminate_verified
        self.store.run_live = lambda d: True        # it looks live until the end is tried
        tried = []

        def terminate(pid, ct, code=1):
            tried.append(pid)
            self.store.run_live = real_live
            return real_end(pid, ct, code)
        with mock.patch.object(process, 'terminate_verified', terminate):
            out = self.stop(R1)
        self.assertEqual(tried, [p.pid])
        self.assertIsNone(p.poll())
        self.assertFalse(out.stopped)
        self.assertIn('ALREADY GONE', out.lines[0])

    def test_a_process_that_cannot_be_ended_is_a_stated_failure(self):
        p, ctime = self.sleeper()
        path, _ = self.running_record(p.pid, ctime)
        self.gate_run(R1, pid=p.pid, ctime=ctime)
        with mock.patch.object(process, 'terminate_verified', return_value='denied'):
            out = self.stop(R1)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertIn('cannot be ended (denied)', out.lines[0])
        self.assertEqual(result.read_path(path)[0]['verdict'], result.RUNNING)

    def test_a_record_with_no_creation_time_is_never_ended_from_here(self):
        p, _ = self.sleeper()
        self.gate_run(R1, pid=p.pid, ctime=None)
        self.store.run_live = lambda d: True
        with mock.patch.object(process, 'terminate_verified') as terminate:
            out = self.stop(R1)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertIn('no creation time', out.lines[0])
        terminate.assert_not_called()
        self.assertIsNone(p.poll())

    def test_a_lock_record_that_changed_is_never_ended_from_here(self):
        p, ctime = self.sleeper()
        other, other_ctime = self.sleeper()
        self.gate_run(R1, pid=p.pid, ctime=ctime)

        def changed(done, wait_s):
            self.gate_run(R1, pid=other.pid, ctime=other_ctime)
            return False
        with mock.patch.object(stop, '_until', changed), \
                mock.patch.object(process, 'terminate_verified') as terminate:
            out = self.stop(R1)
        self.assertEqual(out.code, G.FAILED, out.lines)
        self.assertIn('lock record changed', out.lines[0])
        terminate.assert_not_called()


class HungWrapper(StopCase):
    REAL_FORCE = True

    def wrapper(self):
        """A real gate run whose wrapper never polls (tests/gate/stop_child.py): (Popen, id, pid
        of the process in its job)."""
        spec = {'root': self.root, 'hw': self.HW, 'kinds_path': self.kinds_path}
        p = subprocess.Popen([sys.executable, '-m', 'tests.gate.stop_child', json.dumps(spec)],
                             cwd=os.path.dirname(steps.KIT_TOOLS), env=self.env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait, 30)
        self.addCleanup(p.kill)
        said = []
        for line in p.stdout:
            said.append(line)
            if line.startswith('{'):
                got = json.loads(line)
                return p, got['id'], got['child']
        self.fail('the wrapper ended first: %s' % ''.join(said))

    def test_a_hung_gate_loses_its_tree_and_nothing_else_and_leaves_stopped(self):
        self.store.settings.stop_s = 0.5
        p, rid, child = self.wrapper()
        child_ctime = process.proc_ctime(child)
        wrapper_ctime = self.store.read('active/%s.json' % rid)['ctime']
        path, _ = self.running_record(p.pid, wrapper_ctime)
        sibling, sibling_ctime = self.sleeper()     # the same program, no part of the run
        said = []
        out = stop.stop_run(rid, self.store, actor=OWNER, say=said.append)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.stopped and out.forced, out.lines)
        self.assertTrue(out.lines[0].startswith('STOPPED: gate run %s' % rid), out.lines)
        self.assertEqual(p.wait(30), 1)             # terminate_verified's exit code
        self.assertFalse(process.alive(child, child_ctime))
        self.assertTrue(process.alive(sibling.pid, sibling_ctime))
        self.assertIsNone(self.store.read('active/%s.json' % rid))
        self.assertEqual(self.store.read('ended/%s.json' % rid)['how'], 'reaped')
        rec = result.read_path(path)[0]
        self.assertEqual((rec['verdict'], rec['code']), ('stopped', G.STOPPED))
        self.assertTrue(rec['summary'].startswith('STOPPED: '), rec)
        self.assertEqual(result.wait(self.repo.root, 'kit/feature', TIP, timeout_s=5)[0], rec)


class RealGate(StopCase):
    GATE = HANGING

    def setUp(self):
        StopCase.setUp(self)
        self.up = os.path.join(self.tmp, 'step-up')
        self.after = os.path.join(self.tmp, 'step-after')
        self.env.update(STEP_UP=self.up, STEP_AFTER=self.after)
        self.wt, self.tip = self.branch('feature', {'a.py': 'x = 1\n'})
        self.done = {}

    def start_gate(self):
        def run():
            self.done['out'] = runner.branch_gate(self.wt, full=True, log_dir=self.logs,
                                                  book_kw=self.book_kw)
        t = threading.Thread(target=run)
        t.start()
        self.addCleanup(t.join, WAIT_S)
        return t

    def gate_id(self):
        end = time.monotonic() + WAIT_S
        while time.monotonic() < end:
            ids = [d['id'] for d in self.store.live_runs() if d.get('gate')]
            if ids:
                return ids[0]
            time.sleep(0.02)
        self.fail('no gate run booked')

    def record(self):
        return result.read(self.repo.root, 'kit/feature')[0]

    def assert_stopped_gate(self, t):
        t.join(WAIT_S)
        self.assertFalse(t.is_alive())
        out = self.done['out']
        self.assertEqual(out.code, G.STOPPED, out.lines)
        self.assertTrue(out.lines[0].startswith('STOPPED: '), out.lines)
        rec = self.record()
        self.assertEqual((rec['verdict'], rec['code'], rec['gate_ok_record']),
                         ('stopped', G.STOPPED, None))
        self.assertEqual(rec['summary'], out.lines[0])
        self.assertIsNone(gate_ok.read(self.repo.root, self.tip)[0])
        self.assertEqual(rerun.load(self.repo.root, self.tip), ([], []))     # no red run either
        got, _ = result.wait(self.repo.root, 'kit/feature', self.tip, timeout_s=5)
        self.assertEqual(got['verdict'], 'stopped')     # a waiter ends, and gate.py exits 1 on it
        self.force.assert_not_called()

    def test_a_stop_ends_the_step_runs_no_other_and_leaves_stopped(self):
        t = self.start_gate()
        end = time.monotonic() + WAIT_S
        while not os.path.exists(self.up) and time.monotonic() < end:
            time.sleep(0.02)
        self.assertTrue(os.path.exists(self.up))
        rid = self.gate_id()
        out = self.stop(rid, root=self.wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.stopped, out.lines)
        self.assertEqual(out.lines[0], 'STOPPED: gate run %s (%s/%s): its process tree is ended '
                         'and its lock is free' % (rid, self.store.read('ended/%s.json' % rid)[
                             'repo'], self.store.read('ended/%s.json' % rid)['slug']))
        self.assertEqual(out.result['verdict'], 'stopped')
        self.assert_stopped_gate(t)
        self.assertFalse(os.path.exists(self.after))    # `continue` on the step does not matter
        ended = self.store.read('ended/%s.json' % rid)
        self.assertEqual((ended['how'], ended['code']), ('cancelled', 1))
        again = self.stop(rid)                          # a second stop: already gone
        self.assertEqual(again.code, G.OK, again.lines)
        self.assertIn('had ended (cancelled, code 1)', again.lines[0])

    def test_a_stop_while_the_gate_waits_for_the_lock_leaves_stopped(self):
        usable = self.confine(1)
        if usable[1:]:              # one slot to fight over: the blocker takes it
            now = utc_now()
            hl.create(self.store, self.layout(), {
                'owner': 'owner', 'scope': 'slots', 'start': now,
                'end': now + datetime.timedelta(hours=1),
                'slots': {'cpu': usable[1:], 'gpu': []}}, OWNER, None)
        blocker = lockrun.book('test', 'kit', 'blocker', cores=1, store=self.store, env=self.env,
                               no_wait=True)
        self.addCleanup(blocker.release)
        t = self.start_gate()
        rid = self.gate_id()
        out = self.stop(rid, root=self.wt)
        self.assertEqual(out.code, G.OK, out.lines)
        self.assertTrue(out.stopped, out.lines)
        self.assert_stopped_gate(t)
        self.assertIn('while it waited', self.done['out'].lines[0])
        self.assertFalse(os.path.exists(self.up))       # no step ran


class StopAfterTheSteps(StopCase):
    def test_a_stop_after_the_last_step_is_still_no_pass(self):
        wt, tip = self.branch('feature', {'a.py': 'x = 1\n'})
        real = runner.window_results

        def windows(slot, say, seen=None):
            lockrun.request_stop(self.store, slot.id, 'test')
            return real(slot, say, seen)
        with mock.patch.object(runner, 'window_results', windows):
            out = runner.branch_gate(wt, full=True, log_dir=self.logs, book_kw=self.book_kw)
        self.assertEqual(out.code, G.STOPPED, out.lines)
        self.assertIsNone(gate_ok.read(self.repo.root, tip)[0])
        self.assertEqual(result.read(self.repo.root, 'kit/feature')[0]['verdict'], 'stopped')


class Verdicts(unittest.TestCase):
    def test_stopped_has_its_own_code_and_verdict(self):
        self.assertEqual(G.NAMES[G.STOPPED], 'STOPPED')
        self.assertEqual(result.verdict_of(G.STOPPED), 'stopped')
        self.assertNotIn(G.STOPPED, (6, 7, 8, 10, 11, 12, 130))         # the lock's and wait-result's
        self.assertNotIn(G.STOPPED, (9, 13, 14, 15))                    # a load node's: node.py gate
                                                                        # passes a gate's code on

    def test_a_stopped_summary_is_the_verdict_line(self):
        out = runner.stopped_outcome('gate run r was stopped', 'logs-here')
        self.assertEqual(result._summary(G.Outcome(out.code, ['header'] + out.lines)),
                         out.lines[0])
        self.assertEqual((out.record, out.log_dir), (None, 'logs-here'))

    def test_only_a_gate_s_own_booking_reads_the_lock_s_words_as_stopped(self):
        waited = LockError(1, 'run r was cancelled by stop while it waited; the command was not '
                              'run')
        self.assertEqual(runner.book_refusal(waited).code, G.STOPPED)
        self.assertEqual(runner.lock_refusal(waited).code, 1)           # a landing's way
        self.assertTrue(runner.lock_refusal(waited).lines[0].startswith('NOT RUN: '))
        other = LockError(8, 'NOT ACQUIRED: timed out')
        self.assertEqual(runner.book_refusal(other).lines, runner.lock_refusal(other).lines)


class FinishStopped(StopCase):
    def rewrite(self, path, rec, **fields):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(dict(rec, **fields), f)

    def test_it_replaces_only_this_run_s_running_or_error_record(self):
        for verdict in (result.RUNNING, result.ERROR):
            with self.subTest(verdict):
                path, rec = self.running_record(1, 2)
                self.rewrite(path, rec, verdict=verdict)
                got = result.finish_stopped(rec, path, 'STOPPED: why')
                self.assertEqual((got['verdict'], got['code'], got['summary'], got['run_id']),
                                 ('stopped', G.STOPPED, 'STOPPED: why', rec['run_id']))
                self.assertEqual(result.read_path(path)[0], got)

    def test_a_final_or_newer_record_is_left_alone(self):
        path, rec = self.running_record(1, 2)
        self.rewrite(path, rec, verdict='pass', code=0)
        self.assertEqual(result.finish_stopped(rec, path, 'x')['verdict'], 'pass')
        self.rewrite(path, rec, run_id='newer')
        self.assertEqual(result.finish_stopped(rec, path, 'x')['run_id'], 'newer')
        self.assertEqual(result.read_path(path)[0]['verdict'], result.RUNNING)

    def test_machine_records_finds_a_record_from_no_checkout(self):
        path, _ = self.running_record(1, 2)
        self.assertEqual([p for p, _ in result.machine_records('gate')], [path])
        self.assertEqual(result.machine_records('land'), [])

    def test_machine_records_is_empty_with_no_result_folder(self):
        with mock.patch.object(result, 'machine_root', side_effect=result.ResultError('none')):
            self.assertEqual(result.machine_records('gate'), [])


class Entry(unittest.TestCase):
    def test_stop_needs_exactly_one_run(self):
        for args in (('stop',), ('stop', 'r-1', 'r-2')):
            rc, out, err = gate_py(*args)
            self.assertEqual(rc, G.USAGE, (args, out, err))
            self.assertIn('USAGE:', out + err)

    def test_stop_prints_the_outcome_and_exits_with_its_code(self):
        spec = importlib.util.spec_from_file_location('gate_tool_under_test', GATE_PY)
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        printed = io.StringIO()
        with mock.patch.object(tool.stopmod, 'stop_run',
                               return_value=G.Outcome(G.REFUSED, ['REFUSED: no'])) as stop_run, \
                mock.patch.object(tool, 'Store') as store, contextlib.redirect_stdout(printed):
            code = tool.main(['stop', R1, '-C', 'here'])
        self.assertEqual(code, G.REFUSED)
        self.assertEqual(printed.getvalue(), 'REFUSED: no\n')
        args, kw = stop_run.call_args
        self.assertEqual(args, (R1, store.return_value))
        self.assertEqual(kw['root'], os.path.abspath('here'))


if __name__ == '__main__':
    unittest.main()
