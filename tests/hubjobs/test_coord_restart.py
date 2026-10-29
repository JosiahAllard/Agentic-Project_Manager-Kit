"""kitlib.hubjobs.coord_restart: the restart decision and the job around it."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from kitlib.hubjobs import coord_restart as C
from kitlib.hubsite import jobs
from kitlib.launcher import settings as lsettings

NOW = 1_800_000_000.0
ENTRY = {'repo': 'kit', 'agent': True, 'stem': 'kit-coord', 'state': 'x.md', 'account': 1,
         'name': 'kit-coord-a1'}
OK = {'1': C.AccountState(True, None), '2': C.AccountState(True, None)}


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def usage(pct, resets_in=3600):
    return {'updated_at': iso(NOW - 60),
            'five_hour': {'used_percentage': pct, 'resets_at': iso(NOW + resets_in)},
            'seven_day': {'used_percentage': 10, 'resets_at': iso(NOW + 86400)}}


def decide(entry=ENTRY, live=(), record=None, last_stop=None, down_runs=2, accounts=OK, now=NOW):
    return C.decide('kit', entry, set(live), record, last_stop, down_runs, accounts, now)


class Decide(unittest.TestCase):
    def test_a_coordinator_down_twice_starts_on_its_sessions_json_account(self):
        d = decide()
        self.assertTrue(d.start)
        self.assertEqual(d.account, '1')
        self.assertEqual(d.word, 'restarted-unchecked')   # no usage file: the limit not checked

    def test_the_account_last_seen_running_wins_over_sessions_json(self):
        d = decide(record={'seen_at': NOW - 900, 'account': '2'})
        self.assertEqual((d.start, d.account), (True, '2'))

    def test_a_live_session_of_the_stem_is_never_a_second_copy(self):
        d = decide(live=['kit-coord-a2-r3'])
        self.assertFalse(d.start)
        self.assertEqual(d.reason, 'running')

    def test_a_plain_session_is_not_restarted(self):
        self.assertFalse(decide(entry=dict(ENTRY, agent=False)).start)

    def test_autostart_false_is_not_restarted(self):
        self.assertFalse(decide(entry=dict(ENTRY, autostart=False)).start)

    def test_one_down_sighting_is_not_enough(self):
        d = decide(down_runs=1)
        self.assertFalse(d.start)
        self.assertIsNone(d.word)

    def test_a_stop_with_no_successor_after_the_last_sighting_is_deliberate(self):
        d = decide(record={'seen_at': NOW - 900, 'account': '1'}, last_stop=NOW - 600)
        self.assertFalse(d.start)
        self.assertIn('on purpose', d.reason)

    def test_a_deliberate_stop_with_no_sighting_at_all_stays_down(self):
        self.assertFalse(decide(last_stop=NOW - 86400 * 30).start)

    def test_a_stop_older_than_the_last_sighting_does_not_hold_a_crash(self):
        d = decide(record={'seen_at': NOW - 300, 'account': '1'}, last_stop=NOW - 86400)
        self.assertTrue(d.start)

    def test_an_account_over_the_limit_holds_and_says_over_limit(self):
        d = decide(accounts={'1': C.AccountState(True, usage(95))})
        self.assertFalse(d.start)
        self.assertEqual((d.word, d.account), ('over-limit', '1'))

    def test_an_account_under_the_limit_starts_as_restarted(self):
        d = decide(accounts={'1': C.AccountState(True, usage(50))})
        self.assertEqual((d.start, d.word), (True, 'restarted'))

    def test_a_window_that_has_reset_is_not_over(self):
        self.assertTrue(decide(accounts={'1': C.AccountState(True, usage(99, resets_in=-60))}).start)

    def test_an_account_not_signed_in_holds(self):
        d = decide(accounts={'1': C.AccountState(False, None)})
        self.assertEqual((d.start, d.word), (False, 'not-signed-in'))

    def test_an_account_the_launcher_does_not_know_holds(self):
        self.assertEqual(decide(accounts={}).word, 'not-signed-in')

    def test_no_account_known_holds(self):
        e = dict(ENTRY)
        del e['account']
        d = decide(entry=e)
        self.assertFalse(d.start)
        self.assertIsNone(d.word)

    def test_backoff_after_a_start(self):
        rec = {'starts': [NOW - 300]}
        self.assertIn('backoff', decide(record=rec).reason)
        self.assertTrue(decide(record={'starts': [NOW - C.BACKOFF_S[0]]}).start)

    def test_backoff_grows_with_the_starts(self):
        rec = {'starts': [NOW - 7200, NOW - 1000]}
        self.assertIn('backoff', decide(record=rec).reason)       # 1000 < BACKOFF_S[1]
        self.assertTrue(decide(record={'starts': [NOW - 7200, NOW - C.BACKOFF_S[1]]}).start)

    def test_the_cap_gives_up_and_says_so(self):
        rec = {'starts': [NOW - 20000, NOW - 15000, NOW - 9000]}
        d = decide(record=rec)
        self.assertEqual((d.start, d.word), (False, 'gave-up'))

    def test_starts_older_than_the_window_do_not_count(self):
        old = NOW - C.CAP_WINDOW_S - 1
        self.assertTrue(decide(record={'starts': [old, old, old]}).start)


class Parts(unittest.TestCase):
    def test_stop_times_keeps_only_stops_with_no_successor(self):
        text = ("2026-10-28 10:00:00 stopped 'kit-coord-a1' claude PID 1, window PID 2, conhost PID 3; "
                "successor '-'\n"
                "2026-10-28 11:00:00 stopped 'kit-coord-a2' claude PID 4, window PID -, conhost PID -; "
                "successor 'kit-coord-a2-r2'\n"
                "garbage line\n")
        stops = C.stop_times(text, epoch=lambda s: {'2026-10-28 10:00:00': 10.0}.get(s, 99.0))
        self.assertEqual(stops, {'kit-coord-a1': 10.0})

    def test_of_stem_is_the_stem_or_its_account_names(self):
        self.assertTrue(C.of_stem('kit-coord-a2-r3', 'kit-coord'))
        self.assertTrue(C.of_stem('kit-coord', 'kit-coord'))
        self.assertFalse(C.of_stem('kit-coord-docs-a1', 'kit-coord'))
        self.assertFalse(C.of_stem('kit-coordx-a1', 'kit-coord'))

    def test_stem_stop_takes_the_newest_of_the_stem(self):
        self.assertEqual(C.stem_stop({'kit-coord-a1': 5, 'kit-coord-a2': 9, 'other-a1': 50}, 'kit-coord'), 9)
        self.assertIsNone(C.stem_stop({'other-a1': 50}, 'kit-coord'))

    def test_last_account(self):
        self.assertEqual(C.last_account(ENTRY, {'account': '3'}), '3')
        self.assertEqual(C.last_account(ENTRY, None), '1')
        self.assertIsNone(C.last_account({}, {}))


def proc(name):
    return {'Name': 'claude.exe', 'ProcessId': 10, 'ExecutablePath': r'C:\x\claude.exe',
            'CommandLine': 'claude.exe --remote-control %s -n %s' % (name, name)}


class Ctx(object):
    def __init__(self):
        self.stop = threading.Event()
        self.events = []

    def event(self, word, detail=None):
        self.events.append((word, detail))


class Job(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, 'state')
        self.acct = {n: os.path.join(self.tmp, 'a' + n) for n in ('1', '2')}
        for d in [self.state] + list(self.acct.values()):
            os.makedirs(d)
        for d in self.acct.values():
            open(os.path.join(d, '.credentials.json'), 'w').close()
        self.sessions = {'_note': 'x', 'kit': dict(ENTRY),
                         'plain': {'repo': 'kit', 'agent': False, 'stem': 'plain', 'account': 1,
                                   'name': 'plain-a1'}}
        self.write('sessions.json', self.sessions)
        self.settings = lsettings.Settings({'state_dir': self.state,
                                            'accounts': {n: [d] for n, d in self.acct.items()}},
                                           home=self.tmp)
        self.live = []
        self.calls = []
        self.code = 0
        self.now = NOW
        self.job = C.RestartJob(self.settings, procs=lambda: [proc(n) for n in self.live],
                                child=self.child, clock=lambda: self.now, kit=self.tmp)

    def write(self, name, data):
        with open(os.path.join(self.state, name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def child(self, argv, cwd):
        self.calls.append(argv)
        if isinstance(self.code, Exception):
            raise self.code
        return self.code, ['said']

    def records(self):
        with open(os.path.join(self.state, C.STATE_FILE), encoding='utf-8') as f:
            return json.load(f)

    def run_job(self, ctx=None):
        self.ctx = ctx or Ctx()
        return self.job(self.ctx)

    def test_make_needs_a_state_dir(self):
        class S(object):
            home = self.tmp
        with mock.patch.object(lsettings, 'load', lambda home=None: lsettings.Settings({}, home=home)):
            self.assertIsNone(C.make(S()))
        with mock.patch.object(lsettings, 'load', lambda home=None: self.settings):
            job = C.make(S())
        self.assertEqual((job.name, job.every_s, job.first_s), (C.NAME, C.EVERY_S, C.FIRST_S))

    def test_a_down_coordinator_starts_on_the_second_run_and_only_once(self):
        self.live = ['kit-coord-a2']
        self.assertIsInstance(self.run_job(), jobs.Skip)          # seen running on account 2
        self.live = []
        self.now += 300
        self.assertIsInstance(self.run_job(), jobs.Skip)          # down once
        self.assertEqual(self.calls, [])
        self.now += 300
        note = self.run_job()
        self.assertIn('kit: started on account 2', note)
        self.assertEqual(self.calls[0][-5:], ['start', '--session', 'kit', '--account', '2'])
        self.assertEqual(self.ctx.events, [('restarted-unchecked', 'kit:a2')])
        self.assertEqual(self.records()['kit']['starts'], [self.now])
        self.now += 300
        self.run_job()                                            # backoff: no second start
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ctx.events, [])

    def test_the_plain_session_is_never_started(self):
        for _ in range(3):
            self.now += 300
            self.run_job()
        self.assertTrue(all(c[-3] == 'kit' for c in self.calls))

    def test_a_deliberate_stop_is_left_down(self):
        self.live = ['kit-coord-a1']
        self.run_job()
        self.live = []
        stamp = datetime.datetime.fromtimestamp(self.now + 60).strftime('%Y-%m-%d %H:%M:%S')
        with open(os.path.join(self.state, 'stop.log'), 'w', encoding='utf-8') as f:
            f.write("%s stopped 'kit-coord-a1' claude PID 1, window PID 2, conhost PID 3; successor '-'\n"
                    % stamp)
        for _ in range(3):
            self.now += 300
            note = self.run_job()
        self.assertEqual(self.calls, [])
        self.assertIn('on purpose', note.note)

    def test_over_the_limit_is_pushed_once_and_not_started(self):
        with open(os.path.join(self.acct['1'], 'usage-status.json'), 'w', encoding='utf-8') as f:
            json.dump(usage(97), f)
        ctx = Ctx()
        for _ in range(4):
            self.now += 300
            self.run_job(ctx)
        self.assertEqual(self.calls, [])
        self.assertEqual(ctx.events, [('over-limit', 'kit:a1')])

    def test_a_failed_start_is_a_failure_and_counts_toward_the_cap(self):
        self.code = 2
        self.now += 300
        self.run_job()
        self.now += 300
        with self.assertRaises(C.StartFailed):
            self.run_job()
        self.assertEqual(len(self.records()['kit']['starts']), 1)

    def test_a_start_that_hangs_is_a_failure(self):
        self.code = subprocess.TimeoutExpired('x', 1)
        self.now += 300
        self.run_job()
        self.now += 300
        with self.assertRaises(C.StartFailed):
            self.run_job()

    def test_the_cap_survives_a_hub_restart_and_gives_up(self):
        self.code = 2
        ctx = Ctx()
        for _ in range(60):                 # five hours of runs, a fresh job object each hour
            if _ % 12 == 0:
                self.job = C.RestartJob(self.settings, procs=lambda: [], child=self.child,
                                        clock=lambda: self.now, kit=self.tmp)
            self.now += 300
            try:
                self.job(ctx)
            except C.StartFailed:
                pass
        self.assertEqual(len(self.calls), C.START_CAP)
        self.assertIn(('gave-up', 'kit'), ctx.events)

    def test_a_bad_state_file_is_a_failure_not_an_empty_cap(self):
        with open(os.path.join(self.state, C.STATE_FILE), 'w') as f:
            f.write('{not json')
        with self.assertRaises(Exception):
            self.run_job()

    def test_a_coordinator_that_comes_back_resets_the_down_count(self):
        self.now += 300
        self.run_job()                      # down once
        self.live = ['kit-coord-a1']
        self.now += 300
        self.run_job()
        self.live = []
        self.now += 300
        self.run_job()                      # down once again, not twice
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
