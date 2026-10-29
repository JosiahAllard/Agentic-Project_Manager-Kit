"""kitlib.hubjobs.session_watch: pm_watch's checks as a hub timed job."""
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest import mock

from kitlib import pmwatch as W
from kitlib.hubjobs import session_watch as SW
from kitlib.hubsite import jobs

T0 = 1_800_000_000.0


class FakeHost(object):
    PRELOAD = ()

    def __init__(self, alerts):
        self.alerts = alerts
        self.mtimes = {}
        self.broken = False

    def default_alerts_dir(self):
        return self.alerts

    def newest_finder(self):
        def newest(name):
            if self.broken:
                raise OSError('transcripts unreadable')
            return self.mtimes.get(name)
        return newest

    def rc_records(self, dirs):
        return []


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.alerts = os.path.join(self.dir, 'alerts')
        os.makedirs(self.alerts)
        self.sessions = os.path.join(self.dir, 'sessions.json')
        with open(self.sessions, 'w', encoding='utf-8') as f:
            json.dump({'a': {'name': 'coord-a'}}, f)
        self.state = os.path.join(self.dir, 'state.json')
        self.cfg = {'sessions_file': self.sessions, 'state_file': self.state}
        self.host = FakeHost(self.alerts)
        self.host.mtimes['coord-a'] = T0          # active: no IDLE unless a test moves it
        self.now = T0
        self.pushed = []

    def job(self):
        return SW.SessionWatch(self.host, config=lambda: self.cfg, notice_dir=lambda: None,
                               usage=lambda: {}, rc=False, clock=lambda: self.now)

    def ctx(self):
        return jobs.Ctx(threading.Event(), lambda word, detail=None: self.pushed.append((word, detail)))

    def alert(self, run_id, reason='exit 1'):
        with open(os.path.join(self.alerts, run_id + '.json'), 'w', encoding='utf-8') as f:
            json.dump({'id': run_id, 'reason': reason}, f)

    def beat(self, at):
        W.write_heartbeat(W.heartbeat_file(self.state), 4242, at, False)


class Baseline(Base):
    def test_first_run_is_silent_and_saves_own_state(self):
        self.host.mtimes['coord-a'] = T0 - 3600     # already quiet: baseline
        self.alert('old')                           # already there: baseline
        note = self.job()(self.ctx())
        self.assertEqual(self.pushed, [])
        self.assertEqual(note, 'no events yet')
        self.assertTrue(os.path.exists(self.state + SW.STATE_SUFFIX))
        self.assertFalse(os.path.exists(self.state))  # pm_watch's own file is never written

    def test_idle_pushes_once_then_not_again(self):
        self.host.mtimes['coord-a'] = T0
        j = self.job()
        j(self.ctx())
        self.now = T0 + 30 * 60
        note = j(self.ctx())
        self.assertEqual(self.pushed, [('idle', 'coord-a')])
        self.assertIn('IDLE coord-a', note)
        self.now += 300
        j(self.ctx())
        self.assertEqual(self.pushed, [('idle', 'coord-a')])

    def test_restart_does_not_repeat(self):
        self.host.mtimes['coord-a'] = T0
        j = self.job()
        j(self.ctx())
        self.alert('r1')
        self.now = T0 + 30 * 60
        j(self.ctx())
        self.assertEqual(len(self.pushed), 1)
        self.now += 300
        self.job()(self.ctx())                      # a new hub process, same state file
        self.assertEqual(len(self.pushed), 1)

    def test_unreadable_state_is_a_fresh_baseline(self):
        with open(self.state + SW.STATE_SUFFIX, 'w', encoding='utf-8') as f:
            json.dump([1, 2], f)
        self.alert('old')
        self.job()(self.ctx())
        self.assertEqual(self.pushed, [])


class Pushes(Base):
    def test_one_event_names_kind_and_subject(self):
        j = self.job()
        j(self.ctx())
        self.alert('run7')
        self.now += 60
        note = j(self.ctx())
        self.assertEqual(self.pushed, [('bgfail', 'run7')])
        self.assertIn('BGFAIL run run7 exit 1', note)

    def test_several_events_are_one_push_with_the_count(self):
        j = self.job()
        j(self.ctx())
        self.alert('r1')
        self.alert('r2')
        self.now += 60
        note = j(self.ctx())
        self.assertEqual(self.pushed, [('events', '2')])
        self.assertIn('BGFAIL run r1', note)
        self.assertIn('BGFAIL run r2', note)

    def test_note_keeps_only_the_last_recent_max(self):
        j = self.job()
        j(self.ctx())
        for i in range(SW.RECENT_MAX + 5):
            self.alert('r%03d' % i)
        self.now += 60
        note = j(self.ctx())
        self.assertNotIn('r000', note)
        self.assertIn('r%03d' % (SW.RECENT_MAX + 4), note)

    def test_push_of(self):
        self.assertEqual(SW.push_of(['STOPPED coord-a at 2026-10-29T06:00:00']), ('stopped', 'coord-a'))
        self.assertEqual(SW.push_of(['USAGE account 2 seven_day 95% (now 96%)']), ('usage', '2'))
        self.assertEqual(SW.push_of(['RESET account codex (weekly 90% -> 3%)']), ('reset', 'codex'))
        self.assertEqual(SW.push_of(['LOCKNOTICE n1 window']), ('locknotice', 'n1'))
        self.assertEqual(SW.push_of(['REMOTE pm link lost: x']), ('remote', 'pm'))
        self.assertEqual(SW.push_of(['ERROR idle check: boom']), ('error', 'idle'))
        self.assertEqual(SW.push_of(['ERROR idle: no transcript for x']), ('error', 'idle'))
        self.assertEqual(SW.push_of(['a', 'b', 'c']), ('events', '3'))


class YieldsToPmWatch(Base):
    def test_live_pm_watch_gets_no_push_but_state_advances(self):
        j = self.job()
        j(self.ctx())
        self.alert('r1')
        self.now += 60
        self.beat(self.now - 10)
        out = j(self.ctx())
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('pm_watch is live (pid 4242): 1 event(s)', out.note)
        self.assertEqual(self.pushed, [])
        self.now += SW.LIVE_S + 60                  # pm_watch stopped: the job takes over
        j(self.ctx())
        self.assertEqual(self.pushed, [])           # r1 was pm_watch's, never pushed late
        self.alert('r2')
        self.now += 60
        j(self.ctx())
        self.assertEqual(self.pushed, [('bgfail', 'r2')])

    def test_a_fresh_beat_that_yielded_does_not_hold_the_push(self):
        j = self.job()
        j(self.ctx())
        W.write_heartbeat(W.heartbeat_file(self.state), 4242, self.now - 10, False, yielded=True)
        self.alert('r1')
        self.now += 60
        j(self.ctx())
        self.assertEqual(self.pushed, [('bgfail', 'r1')], 'a pm_watch that stepped aside is not waited for')

    def test_a_fresh_beat_not_yielded_or_from_an_older_pm_watch_holds_the_push(self):
        j = self.job()
        j(self.ctx())
        hb = W.heartbeat_file(self.state)
        W.write_heartbeat(hb, 4242, self.now - 10, False, yielded=False)
        self.alert('r1')
        self.now += 60
        self.assertIsInstance(j(self.ctx()), jobs.Skip)
        with open(hb, 'w', encoding='utf-8') as f:  # an older pm_watch: no "yielded" field
            json.dump({'pid': 4242, 'beat': self.now - 10, 'stream': False, 'started': None}, f)
        self.alert('r2')
        self.now += 60
        self.assertIsInstance(j(self.ctx()), jobs.Skip)
        self.assertEqual(self.pushed, [])

    def test_stale_heartbeat_does_not_hold_the_push(self):
        j = self.job()
        j(self.ctx())
        self.beat(self.now - SW.LIVE_S - 1)
        self.alert('r1')
        self.now += 60
        j(self.ctx())
        self.assertEqual(self.pushed, [('bgfail', 'r1')])


class Failures(Base):
    def test_failing_check_gives_error_and_the_job_lives(self):
        self.host.mtimes['coord-a'] = T0
        j = self.job()
        j(self.ctx())
        self.host.broken = True
        for _ in range(3):
            self.now += 300                         # the idle check's own cadence
            self.assertNotIsInstance(j(self.ctx()), jobs.Skip)
        self.assertEqual(self.pushed, [('error', 'idle')])
        self.alert('r1')                            # the other checks still run
        self.now += 60
        j(self.ctx())
        self.assertEqual(self.pushed[-1], ('bgfail', 'r1'))

    def test_state_write_failure_is_a_job_failure_after_the_push(self):
        runner = jobs.Jobs([jobs.Job(SW.NAME, SW.EVERY_S, self.job())],
                           notify=lambda name, word, detail=None: self.pushed.append((word, detail)))
        runner.run_now(SW.NAME)
        os.remove(self.state + SW.STATE_SUFFIX)
        os.makedirs(self.state + SW.STATE_SUFFIX)    # os.replace onto a folder: OSError
        self.alert('r1')
        self.now += 60
        runner.run_now(SW.NAME)
        self.assertEqual(self.pushed, [('bgfail', 'r1')])
        st = runner.status()[SW.NAME]
        self.assertEqual(st['failures'], 1)
        self.assertIn('Error', st['error'])

    def test_start_failure_is_retried_next_run(self):
        calls = []

        def usage():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError('the usage report printed no account lines')
            return {}
        j = SW.SessionWatch(self.host, config=lambda: self.cfg, notice_dir=lambda: None,
                            usage=usage, rc=False, clock=lambda: self.now)
        with self.assertRaises(RuntimeError):
            j(self.ctx())
        self.now += 60
        self.assertEqual(j(self.ctx()), 'no events yet')

    def test_no_sessions_file_is_a_skip(self):
        self.cfg = {}
        out = self.job()(self.ctx())
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('sessions_file', out.note)


class Make(unittest.TestCase):
    def test_none_when_the_host_has_no_readers(self):
        with mock.patch.object(SW, 'host_module', side_effect=ImportError('no claude_host')):
            self.assertIsNone(SW.make(None))

    def test_job_on_the_default_host(self):
        job = SW.make(None)
        self.assertEqual((job.name, job.every_s), (SW.NAME, SW.EVERY_S))


if __name__ == '__main__':
    unittest.main()
