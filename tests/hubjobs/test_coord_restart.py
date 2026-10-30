"""kitlib.hubjobs.coord_restart: the restart decision and the job around it."""
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.hubjobs import coord_restart as C
from kitlib.hubsite import jobs
from kitlib.launcher import settings as lsettings
from kitlib.launcher.settings import LaunchError

NOW = 1_800_000_000.0
ENTRY = {'repo': 'kit', 'agent': True, 'stem': 'kit-coord', 'state': 'x.md', 'account': 1,
         'name': 'kit-coord-a1'}
OK = {'1': C.AccountState(True, None), '2': C.AccountState(True, None)}


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def usage(pct, resets_in=3600, week_in=86400, age=60):
    return {'updated_at': iso(NOW - age),
            'five_hour': {'used_percentage': pct, 'resets_at': iso(NOW + resets_in)},
            'seven_day': {'used_percentage': 10, 'resets_at': iso(NOW + week_in)}}


def decide(entry=ENTRY, live=(), record=None, last_stop=None, down_runs=2, accounts=OK, now=NOW,
           **wake):
    return C.decide('kit', entry, set(live), record, last_stop, down_runs, accounts, now, **wake)


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

    def test_a_stop_stamped_within_a_second_before_the_sighting_is_deliberate(self):
        # stop.log stamps whole seconds: a stop at seen_at + 0.4 is written as seen_at - 0.6
        d = decide(record={'seen_at': NOW + 0.6, 'account': '1'}, last_stop=NOW)
        self.assertIn('on purpose', d.reason)
        self.assertTrue(decide(record={'seen_at': NOW + 1, 'account': '1'}, last_stop=NOW).start)

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

    def test_a_fresh_account_under_the_limit_whose_week_resets_first_wins_over_the_last(self):
        d = decide(accounts={'1': C.AccountState(True, usage(50, week_in=3 * 86400)),
                             '2': C.AccountState(True, usage(50, week_in=86400))})
        self.assertEqual((d.start, d.account, d.word), (True, '2', 'restarted'))
        self.assertIn('resets first', d.reason)

    def test_with_no_fresh_account_under_the_limit_the_last_account_rule_is_unchanged(self):
        stale = usage(10, week_in=3600, age=2 * 3600)                 # resets first, but 2 h old
        d = decide(accounts={'1': C.AccountState(True, None), '2': C.AccountState(True, stale)})
        self.assertEqual((d.start, d.account, d.word), (True, '1', 'restarted-unchecked'))
        d = decide(accounts={'1': C.AccountState(True, usage(50, age=2 * 3600)),
                             '2': C.AccountState(True, stale)})
        self.assertEqual((d.start, d.account, d.word), (True, '1', 'restarted'))   # stale but under: as before
        d = decide(accounts={'1': C.AccountState(True, usage(95)), '2': C.AccountState(True, stale)})
        self.assertEqual((d.start, d.account, d.word), (False, '1', 'over-limit'))
        d = decide(accounts={'1': C.AccountState(False, None), '2': C.AccountState(True, usage(95))})
        self.assertEqual((d.start, d.account, d.word), (False, '1', 'not-signed-in'))

    def test_no_last_account_known_still_starts_on_a_fresh_account(self):
        e = dict(ENTRY)
        del e['account']
        d = decide(entry=e, accounts={'1': C.AccountState(True, None), '2': C.AccountState(True, usage(50))})
        self.assertEqual((d.start, d.account, d.word), (True, '2', 'restarted'))

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

    def test_local_epoch_takes_the_later_instant_in_the_repeated_hour(self):
        stamp = '2026-11-01 01:30:00'
        st = time.strptime(stamp, '%Y-%m-%d %H:%M:%S')
        base = 1_793_500_000.0

        def mktime(t):                      # both guesses name the same wall clock: ambiguous
            return base - (3600 if t[8] == 1 else 0)

        def localtime(t):
            return time.struct_time(tuple(st[:8]) + (1 if t == base - 3600 else 0,))
        self.assertEqual(C.local_epoch(stamp, mktime, localtime), base)

    def test_local_epoch_takes_the_one_guess_that_fits(self):
        stamp = '2026-07-01 12:00:00'
        st = time.strptime(stamp, '%Y-%m-%d %H:%M:%S')

        def mktime(t):
            return 500.0 if t[8] == 1 else 9000.0

        def localtime(t):                   # only the summer guess reads back as its own wall clock
            return time.struct_time(tuple(st[:8]) + (1,)) if t == 500.0 else time.localtime(0)
        self.assertEqual(C.local_epoch(stamp, mktime, localtime), 500.0)

    def test_local_epoch_matches_mktime_on_this_machine(self):
        stamp = '2026-06-15 12:00:00'
        self.assertEqual(C.local_epoch(stamp), time.mktime(time.strptime(stamp, '%Y-%m-%d %H:%M:%S')))

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


class JobBase(unittest.TestCase):
    """The job over a temporary state folder, with its process list, children and clock faked."""

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
        self.uptime = float('inf')      # no boot: the after-reboot one-shot stays out of the way
        self.spawned = []
        self.job = self.new_job()

    def new_job(self):
        return C.RestartJob(self.settings, procs=lambda: [proc(n) for n in self.live],
                            child=self.child, clock=lambda: self.now, kit=self.tmp,
                            uptime=lambda: self.uptime, detached=self.detached)

    def detached(self, argv, cwd):
        if isinstance(self.spawn_error, Exception):
            raise self.spawn_error
        self.spawned.append((argv, cwd))
        return object()

    spawn_error = None

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


class Job(JobBase):
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

    def test_the_usage_file_judged_is_the_newest_reading_not_the_newest_write(self):
        second = os.path.join(self.tmp, 'a2b')
        os.makedirs(second)
        self.settings = lsettings.Settings({'state_dir': self.state,
                                            'accounts': {'1': [self.acct['1']], '2': [self.acct['2'], second]}},
                                           home=self.tmp)
        self.job = self.new_job()
        fresh, stale = (os.path.join(d, 'usage-status.json') for d in (self.acct['2'], second))
        for path, data in ((fresh, usage(10, age=60)), (stale, usage(10, age=3 * 3600))):
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        os.utime(fresh, (NOW - 3600, NOW - 3600))
        os.utime(stale, (NOW, NOW))                  # written last, its reading older
        for _ in range(2):
            self.now += 300
            self.run_job()
        self.assertEqual(self.calls[0][-5:], ['start', '--session', 'kit', '--account', '2'])

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
                                        clock=lambda: self.now, kit=self.tmp,
                                        uptime=lambda: float('inf'), detached=self.detached)
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
        with self.assertRaises(LaunchError):
            self.run_job()

    def add_ret(self, **extra):
        self.sessions['ret'] = dict({'repo': 'ret', 'agent': True, 'stem': 'ret-coord', 'state': 'y.md',
                                     'account': 2, 'name': 'ret-coord-a2'}, **extra)
        self.write('sessions.json', self.sessions)

    def over(self, *accounts):
        for n in accounts:
            with open(os.path.join(self.acct[n], 'usage-status.json'), 'w', encoding='utf-8') as f:
                json.dump(usage(97), f)

    def test_two_coordinators_over_the_limit_in_one_run_reach_one_push(self):
        self.add_ret()
        self.over('1', '2')
        ctx = Ctx()
        for _ in range(4):
            self.now += 300
            self.run_job(ctx)
        self.assertEqual(self.calls, [])
        self.assertEqual(ctx.events, [('over-limit', 'kit:a1.ret:a2')])

    def test_a_hold_beside_a_start_is_pushed_on_the_next_run(self):
        self.add_ret()
        self.over('1')                      # kit holds over-limit, ret starts on account 2
        ctx = Ctx()
        for _ in range(3):
            self.now += 300
            self.run_job(ctx)
        self.assertEqual(ctx.events, [('restarted-unchecked', 'ret:a2'), ('over-limit', 'kit:a1')])

    def test_keys_past_the_detail_field_go_out_on_a_later_run(self):
        long_key = 'k' * 58                 # sorts after 'kit'; with ':a2' it fills the 64-character field
        self.sessions[long_key] = dict(ENTRY, stem='long-coord', name='long-coord-a2', account=2)
        self.write('sessions.json', self.sessions)
        self.over('1', '2')
        ctx = Ctx()
        for _ in range(4):
            self.now += 300
            self.run_job(ctx)
        self.assertEqual(ctx.events, [('over-limit', 'kit:a1'), ('over-limit', long_key + ':a2')])
        self.assertTrue(all(len(d) <= C.DETAIL_MAX for _, d in ctx.events))

    def test_two_coordinators_down_start_one_per_run(self):
        self.add_ret()
        self.now += 300
        self.run_job()                      # both down once
        self.now += 300
        note = self.run_job()
        self.assertEqual([c[-3] for c in self.calls], ['kit'])
        self.assertIn('ret: down; decided on the next run', note)
        self.now += 300
        self.run_job()
        self.assertEqual([c[-3] for c in self.calls], ['kit', 'ret'])
        self.assertEqual(self.ctx.events, [('restarted-unchecked', 'ret:a2')])

    def test_no_start_while_the_hub_closes(self):
        self.now += 300
        self.run_job()
        self.now += 300
        ctx = Ctx()
        ctx.stop.set()
        result = self.run_job(ctx)
        self.assertIsInstance(result, jobs.Skip)
        self.assertIn('hub is closing', result.note)
        self.assertEqual(self.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.state, C.STATE_FILE))
                         and self.records().get('kit', {}).get('starts'))

    def test_a_start_after_a_failing_streak_is_pushed_after_recovered(self):
        # the runner pushes `recovered` straight after the run that ends the streak, and the hub
        # keeps one owed push per job: the restart must go out on the next run, not before it
        pushes = []
        runner = jobs.Jobs([jobs.Job(C.NAME, C.EVERY_S, self.job)],
                           notify=lambda name, word, detail=None: pushes.append((word, detail)))
        bad = os.path.join(self.state, C.STATE_FILE)
        with open(bad, 'w') as f:
            f.write('{not json')
        for _ in range(jobs.FAIL_PUSH):
            self.now += 300
            runner.run_now(C.NAME)
        self.assertEqual(pushes, [('failing', None)])
        os.remove(bad)
        for _ in range(2):                  # down once (a Skip keeps the streak), then the start
            self.now += 300
            runner.run_now(C.NAME)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(pushes[-1], ('recovered', None))
        self.now += 300
        runner.run_now(C.NAME)
        self.assertEqual(pushes, [('failing', None), ('recovered', None),
                                  ('restarted-unchecked', 'kit:a1')])

    def test_a_start_with_no_failing_streak_is_pushed_in_its_own_run(self):
        bad = os.path.join(self.state, C.STATE_FILE)
        with open(bad, 'w') as f:
            f.write('{not json')
        for _ in range(jobs.FAIL_PUSH - 1):
            self.now += 300
            with self.assertRaises(LaunchError):
                self.run_job()
        os.remove(bad)
        self.now += 300
        self.run_job()
        self.now += 300
        self.run_job()
        self.assertEqual(self.ctx.events, [('restarted-unchecked', 'kit:a1')])

    def test_a_start_that_cannot_run_is_a_failure(self):
        self.code = OSError('no python')
        self.now += 300
        self.run_job()
        self.now += 300
        with self.assertRaises(C.StartFailed) as cm:
            self.run_job()
        self.assertIn('could not start', str(cm.exception))
        self.assertEqual(len(self.records()['kit']['starts']), 1)

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


class BootAction(unittest.TestCase):
    def test_once_per_boot_at_any_uptime(self):
        self.assertEqual(C.boot_action(None, 240, NOW), C.RUN)
        self.assertEqual(C.boot_action({}, C.BOOT_S - 1, NOW), C.RUN)
        self.assertEqual(C.boot_action({'boot_at': 'x'}, 240, NOW), C.RUN)       # a bad record is none
        self.assertIsNone(C.boot_action(None, float('inf'), NOW))              # uptime unknown: never
        self.assertIsNone(C.boot_action({'boot_at': NOW - 86400}, float('inf'), NOW))
        self.assertIsNone(C.boot_action({'boot_at': NOW - 240 + 30}, 240, NOW))     # this boot, run
        self.assertIsNone(C.boot_action({'boot_at': NOW - 7200 - 100}, 7200, NOW))  # within BOOT_SAME_S
        self.assertEqual(C.boot_action({'boot_at': NOW - 86400}, 240, NOW), C.RUN)  # an earlier boot
        self.assertEqual(C.boot_action({'boot_at': NOW - 86400}, 7200, NOW), C.RUN)  # late sign-in

    def test_clock_drift_over_a_long_uptime_is_still_this_boot(self):
        up = 14 * 86400                                         # two weeks up, 250 s of drift
        self.assertIsNone(C.boot_action({'boot_at': NOW - up - 250}, up, NOW))
        self.assertIsNone(C.boot_action({'boot_at': NOW - up + 250}, up, NOW))

    def test_a_reboot_after_a_long_boot_is_a_new_boot(self):
        up = 14 * 86400                                         # the boot before lasted 16 days
        self.assertEqual(C.boot_action({'boot_at': NOW - up - 16 * 86400}, up, NOW), C.RUN)

    def test_no_boot_kept_and_up_long_only_keeps_the_boot(self):
        self.assertEqual(C.boot_action(None, C.BOOT_S, NOW), C.MARK)
        self.assertEqual(C.boot_action({}, 3 * 3600, NOW), C.MARK)


class AfterBoot(JobBase):
    """The hub, as the only restarter, runs launch.py after-reboot once per boot (plain sessions
    and the PM); the coordinators stay this job's."""

    def setUp(self):
        JobBase.setUp(self)
        self.live = ['kit-coord-a1']                            # the coordinator runs: no start

    def test_the_first_run_after_a_boot_starts_after_reboot_once(self):
        self.uptime = 240
        note = self.run_job()
        self.assertNotIsInstance(note, jobs.Skip)
        self.assertIn('after-reboot started', note)
        (argv, cwd), = self.spawned
        self.assertEqual(argv[-2:], [os.path.join(self.tmp, 'tools', 'launch.py'), 'after-reboot'])
        self.assertEqual(cwd, self.tmp)
        self.assertEqual(self.records()[C.BOOT_KEY], {'boot_at': NOW - 240})
        self.assertEqual(self.calls, [])                        # no coordinator start on this run
        self.now += 300
        self.uptime += 300
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(len(self.spawned), 1)

    def test_a_hub_restart_soon_after_the_boot_does_not_run_it_again(self):
        self.uptime = 240
        self.run_job()
        self.job = self.new_job()                               # the hub restarted
        self.now += 600
        self.uptime += 600
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(len(self.spawned), 1)

    def test_no_boot_kept_and_up_long_keeps_the_boot_without_running_it(self):
        self.uptime = 3 * 3600                                  # a first install, or a lost file
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.spawned, [])
        self.assertEqual(self.records()[C.BOOT_KEY], {'boot_at': NOW - 3 * 3600})
        self.uptime = 240                                       # the same process asks only once
        self.now += 300
        self.run_job()
        self.assertEqual(self.spawned, [])

    def test_a_late_sign_in_still_runs_it(self):
        self.write(C.STATE_FILE, {C.BOOT_KEY: {'boot_at': NOW - 86400}})    # an older boot
        self.uptime = 2 * 3600                                  # the owner signed in 2 h after boot
        note = self.run_job()
        self.assertIn('after-reboot started', note)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.records()[C.BOOT_KEY], {'boot_at': NOW - 2 * 3600})

    def test_a_hub_restart_in_the_same_boot_does_not_run_it_at_any_uptime(self):
        self.write(C.STATE_FILE, {C.BOOT_KEY: {'boot_at': NOW - 2 * 3600}})  # this boot, already run
        self.uptime = 2 * 3600
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.spawned, [])

    def test_a_bad_state_file_still_runs_it_and_writes_nothing(self):
        bad = os.path.join(self.state, C.STATE_FILE)
        with open(bad, 'w') as f:
            f.write('{not json')
        self.uptime = 240
        with self.assertRaises(LaunchError):                    # still a failure: the cap is unread
            self.run_job()
        self.assertEqual(len(self.spawned), 1)
        with open(bad) as f:
            self.assertEqual(f.read(), '{not json')
        self.now += 300
        with self.assertRaises(LaunchError):
            self.run_job()
        self.assertEqual(len(self.spawned), 1)                  # this hub process asks once

    def test_a_bad_sessions_json_still_runs_it(self):
        with open(os.path.join(self.state, 'sessions.json'), 'w') as f:
            f.write('{not json')
        self.uptime = 240
        with self.assertRaises(Exception):
            self.run_job()
        self.assertEqual(len(self.spawned), 1)
        self.assertIn(C.BOOT_KEY, self.records())

    def test_a_child_that_cannot_start_fails_the_run_and_is_tried_again(self):
        self.uptime = 240
        self.spawn_error = OSError('no python')
        with self.assertRaises(C.StartFailed):
            self.run_job()
        self.assertFalse(os.path.exists(os.path.join(self.state, C.STATE_FILE)))
        self.spawn_error = None
        self.now += 300
        self.uptime += 300
        self.run_job()
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.records()[C.BOOT_KEY], {'boot_at': NOW - 240})

    def test_the_boot_record_is_not_a_coordinator(self):
        self.write(C.STATE_FILE, {C.BOOT_KEY: {'boot_at': NOW - 86400}})
        self.live = ['kit-coord-a2']                            # seen on account 2
        self.run_job()
        rec = self.records()
        self.assertEqual(rec[C.BOOT_KEY], {'boot_at': NOW - 86400})
        self.assertEqual(rec['kit']['account'], '2')


WAKE = None                                                 # set by WakeFixture.setUp


class WakeFixture(unittest.TestCase):
    def setUp(self):                                        # per test, so each one is red on the base
        global WAKE
        WAKE = C.Wake('its repo has new startable records: EX-B', ['EX-B'])


STOPPED = {'seen_at': NOW - 900, 'account': '1'}            # with last_stop NOW - 600: deliberate


class WakeDecide(WakeFixture):
    def test_a_wake_lifts_the_deliberate_stop(self):
        d = decide(record=STOPPED, last_stop=NOW - 600, wake=WAKE)
        self.assertEqual((d.start, d.account, d.wake), (True, '1', WAKE))
        self.assertIn('woken (its repo has new startable records: EX-B)', d.reason)

    def test_no_wake_still_holds_the_deliberate_stop(self):
        self.assertIn('on purpose', decide(record=STOPPED, last_stop=NOW - 600).reason)

    def test_a_wake_lifts_no_other_hold(self):
        self.assertEqual(decide(record=STOPPED, last_stop=NOW - 600, wake=WAKE,
                                live=['kit-coord-a1']).reason, 'running')
        self.assertFalse(decide(entry=dict(ENTRY, autostart=False), record=STOPPED,
                                last_stop=NOW - 600, wake=WAKE).start)
        self.assertFalse(decide(record=STOPPED, last_stop=NOW - 600, wake=WAKE, down_runs=1).start)
        capped = dict(STOPPED, starts=[NOW - 20000, NOW - 15000, NOW - 9000])
        self.assertEqual(decide(record=capped, last_stop=NOW - 600, wake=WAKE).word, 'gave-up')
        self.assertIn('backoff', decide(record=dict(STOPPED, starts=[NOW - 300]), last_stop=NOW - 600,
                                        wake=WAKE).reason)
        d = decide(record=STOPPED, last_stop=NOW - 600, wake=WAKE,
                   accounts={'1': C.AccountState(True, usage(95))})
        self.assertEqual((d.start, d.word), (False, 'over-limit'))

    def test_the_account_pick_applies_to_a_wake_start(self):
        d = decide(record=STOPPED, last_stop=NOW - 600, wake=WAKE,
                   accounts={'1': C.AccountState(True, usage(50, week_in=3 * 86400)),
                             '2': C.AccountState(True, usage(50, week_in=86400))})
        self.assertEqual((d.start, d.account, d.wake), (True, '2', WAKE))

    def test_a_crash_restart_carries_no_wake(self):
        d = decide(record={'seen_at': NOW - 300, 'account': '1'}, last_stop=NOW - 86400, wake=WAKE)
        self.assertTrue(d.start)
        self.assertIsNone(d.wake)


class WakeParts(WakeFixture):
    def test_backlog_wake_is_an_id_outside_the_baseline(self):
        w = C.backlog_wake({'EX-A', 'EX-C', 'EX-B'}, ['EX-A'])
        self.assertEqual(w.ids, ['EX-B', 'EX-C'])
        self.assertEqual(w.reason, 'its repo has new startable records: EX-B, EX-C')
        self.assertIsNone(C.backlog_wake({'EX-A'}, ['EX-A', 'EX-Z']))
        self.assertIsNone(C.backlog_wake(set(), []))

    def test_a_long_list_names_five_then_the_rest_as_a_count(self):
        w = C.backlog_wake(['EX-%d' % i for i in range(8)], [])
        self.assertTrue(w.reason.endswith('EX-0, EX-1, EX-2, EX-3, EX-4 +3 more'))
        self.assertEqual(len(w.ids), 8)

    def test_wake_of_takes_the_first_reason(self):
        self.assertIs(C.wake_of(None, WAKE), WAKE)
        self.assertIsNone(C.wake_of(None, None))

    def test_the_wake_prompt_names_the_entry_state_note(self):
        self.assertEqual(C.wake_prompt(WAKE, 'C:/notes/state-kit.md'),
                         'Started by the hub: its repo has new startable records: EX-B. '
                         'Read your state note (C:/notes/state-kit.md), then run backlog next.')
        self.assertTrue(C.wake_prompt(WAKE, '').endswith('Read your state note, then run backlog next.'))

    def test_a_wake_start_passes_a_short_prompt_and_a_plain_start_none(self):
        argv = C.start_argv('K', 'kit', '2', C.wake_prompt(WAKE))
        self.assertEqual(argv[-2], '--prompt')
        self.assertEqual(argv[-1], 'Started by the hub: its repo has new startable records: EX-B. '
                                   'Read your state note, then run backlog next.')
        self.assertEqual(C.start_argv('K', 'kit', '2')[-2:], ['--account', '2'])

    def test_the_sessions_of_a_coordinator(self):
        self.assertEqual(C.wake_sessions('kit', {'repo': 'kitrepo'}), ['kit'])
        self.assertEqual(C.wake_sessions('game', {'repo': 'game'}), ['game', None])


class Rec(object):
    def __init__(self, rid):
        self.id = rid


class Offered(unittest.TestCase):
    """offered_ids over stand-ins for the store: the offer of each session, deduplicated."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, 'repo')
        os.makedirs(self.repo)
        self.settings = lsettings.Settings({'state_dir': self.tmp,
                                            'repos': {'game': self.repo, 'kitrepo': self.repo}},
                                           home=self.tmp)
        self.by_session = {}
        self.opened, self.loads = [], []

    def kit_json(self, data):
        with open(os.path.join(self.repo, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def offered(self, key, entry, pm=(None, None, None)):
        from kitlib.backlog import offer as omod
        from kitlib.backlog import opener, plan
        from kitlib.backlog import store as bstore

        class Store(object):
            def __init__(s, session):
                s.session = session

            def load(s):
                self.loads.append(s.session)
                return 'snap'

        class Ctx(object):
            def __init__(s, root, session):
                s.root, s.remote, s.store, s.session = root, 'origin', Store(session), session

        def open_store(root, fetch=True, session=None, branch=None):
            self.opened.append((root, fetch, session))
            return Ctx(root, session)

        def offer_for(snap, ctx, branch=None, cap=5, seeds=(), holding=None):
            self.assertEqual((snap, cap, holding), ('snap', None, ['pull']))
            return [mock.Mock(rec=Rec(r)) for r in self.by_session.get(ctx.session, ())], [], holding, None
        with mock.patch.object(opener, 'open_store', open_store), \
                mock.patch.object(plan, 'offer_for', offer_for), \
                mock.patch.object(omod, 'in_flight_split', lambda snap, root, remote: (['pull'], [], None)), \
                mock.patch.object(bstore, 'pm_data_live_store', pm if callable(pm) else lambda root: pm):
            return C.offered_ids(self.settings, key, entry)

    def test_a_session_tagged_coordinator_reads_its_own_session_only(self):
        self.kit_json({'backlog': {'store': 'ref'}})
        self.by_session = {'kit': ['EX-A'], None: ['EX-UNTAGGED']}
        self.assertEqual(self.offered('kit', {'repo': 'kitrepo'}), {'EX-A'})
        self.assertEqual(self.opened, [(self.repo, False, 'kit')])     # no fetch

    def test_a_main_coordinator_also_reads_the_untagged_records_once_each(self):
        self.kit_json({'backlog': {'store': 'ref'}})
        self.by_session = {'game': ['EX-R1', 'EX-R2'], None: ['EX-R2', 'EX-R3']}
        self.assertEqual(self.offered('game', {'repo': 'game'}), {'EX-R1', 'EX-R2', 'EX-R3'})
        self.assertEqual([s for _, _, s in self.opened], ['game', None])
        self.assertEqual(len(self.loads), 1)                          # one store read for both

    def test_a_repo_naming_no_store_reads_its_pm_data_worktree(self):
        self.kit_json({})
        pm = os.path.join(self.tmp, 'pm')
        self.by_session = {'kit': ['EX-A']}
        self.assertEqual(self.offered('kit', {'repo': 'kitrepo'}, pm=(pm, 'kit/backlog', None)), {'EX-A'})
        self.assertEqual(self.opened[0][0], pm)

    def test_no_store_named_anywhere_reads_the_repo_itself(self):
        self.offered('kit', {'repo': 'kitrepo'}, pm=(None, None, None))     # no .kit.json at all
        self.assertEqual(self.opened[0][0], self.repo)
        self.opened = []
        self.offered('kit', {'repo': 'kitrepo'}, pm=(os.path.join(self.tmp, 'pm'), None, None))
        self.assertEqual(self.opened[0][0], self.repo)                # its PM data names none either

    def test_a_bug_in_the_pm_data_lookup_is_not_taken_for_unread(self):
        def bug(root):
            raise KeyError('a bug')
        with self.assertRaises(KeyError):
            self.offered('kit', {'repo': 'kitrepo'}, pm=bug)
        self.assertEqual(self.opened, [])

    def test_what_cannot_be_read_is_unread_and_opens_nothing(self):
        with self.assertRaises(C.BacklogUnread):
            self.offered('x', {'repo': 'nowhere'})                    # not in launcher.repos
        with self.assertRaises(C.BacklogUnread):
            self.offered('kit', {'repo': 'kitrepo'}, pm=(None, None, 'no worktree has the branch'))
        self.kit_json('not an object')
        with self.assertRaises(C.BacklogUnread):
            self.offered('kit', {'repo': 'kitrepo'})
        self.assertEqual(self.opened, [])

    def test_a_store_failure_is_unread(self):
        from kitlib.backlog import offer as omod
        from kitlib.backlog.errors import Failure
        self.kit_json({'backlog': {'store': 'ref'}})
        for exc in (Failure('bad scope'), omod.PartitionError('1 != 2')):
            def boom(*a, **k):
                raise exc
            from kitlib.backlog import opener
            with mock.patch.object(opener, 'open_store', boom), self.assertRaises(C.BacklogUnread):
                C.offered_ids(self.settings, 'kit', {'repo': 'kitrepo'})


class WakeJob(JobBase):
    """The job with its backlog read replaced: `self.offer` a set of ids, or an exception."""

    def setUp(self):
        self.offer = set()
        self.reads = []
        super().setUp()

    def new_job(self):
        return C.RestartJob(self.settings, procs=lambda: [proc(n) for n in self.live],
                            child=self.child, clock=lambda: self.now, kit=self.tmp,
                            uptime=lambda: self.uptime, detached=self.detached, backlog=self.backlog)

    def backlog(self, key, entry):
        self.reads.append(key)
        if isinstance(self.offer, Exception):
            raise self.offer
        return set(self.offer)

    def stop(self, name='kit-coord-a1', after=60):
        stamp = datetime.datetime.fromtimestamp(self.now + after).strftime('%Y-%m-%d %H:%M:%S')
        with open(os.path.join(self.state, 'stop.log'), 'a', encoding='utf-8') as f:
            f.write("%s stopped '%s' claude PID 1, window PID 2, conhost PID 3; successor '-'\n"
                    % (stamp, name))

    def held_run(self):
        """Seen running, then stopped on purpose."""
        self.live = ['kit-coord-a1']
        self.run_job()
        self.live = []
        self.stop()

    def test_the_first_held_run_keeps_a_baseline_and_a_new_record_wakes_it(self):
        self.offer = {'EX-A'}
        self.held_run()
        for _ in range(2):
            self.now += 300
            self.run_job()
        self.assertEqual(self.calls, [])                          # EX-A was offered at the stop
        self.assertEqual(self.records()['kit']['held']['ids'], ['EX-A'])
        self.offer = {'EX-A', 'EX-B'}
        self.now += 300
        note = self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][-4:], ['--account', '1', '--prompt',
                                              'Started by the hub: its repo has new startable records: '
                                              'EX-B. Read your state note (x.md), then run backlog '
                                              'next.'])
        self.assertIn('kit: started on account 1 (woken: its repo has new startable records: EX-B)', note)
        self.assertEqual(self.ctx.events, [('restarted-unchecked', 'kit:a1')])

    def test_a_coordinator_that_stops_again_is_not_woken_for_the_same_records(self):
        self.offer = set()
        self.held_run()
        for _ in range(2):                                        # down 1 of 2: not read
            self.now += 300
            self.run_job()                                        # then the baseline: nothing offered
        self.offer = {'EX-B'}
        self.now += 300
        self.run_job()
        self.assertEqual(len(self.calls), 1)                      # woken for EX-B
        self.live = ['kit-coord-a1']
        self.now += 300
        self.run_job()                                            # it came up, found EX-B not for it
        self.assertNotIn('held', self.records()['kit'])
        self.live = []
        self.stop()
        for _ in range(4):
            self.now += 3600
            self.run_job()
        self.assertEqual(len(self.calls), 1)
        self.offer = {'EX-B', 'EX-C'}
        self.now += 3600
        self.run_job()
        self.assertEqual(len(self.calls), 2)
        self.assertIn('EX-C', self.calls[1][-1])
        self.assertNotIn('EX-B', self.calls[1][-1])

    def test_a_backlog_that_cannot_be_read_is_a_note_not_a_failure(self):
        self.held_run()
        self.offer = C.BacklogUnread('the store could not be read (Failure)')
        for _ in range(3):
            self.now += 300
            result = self.run_job()
        self.assertIsInstance(result, jobs.Skip)
        self.assertIn('kit: backlog not read, so not woken: the store could not be read (Failure)', result.note)
        self.assertIn('on purpose', result.note)
        self.assertEqual(self.calls, [])
        self.assertNotIn('held', self.records()['kit'])           # no baseline from a failed read

    def test_a_bug_in_the_backlog_read_fails_the_run(self):
        self.held_run()
        self.offer = KeyError('a bug')                            # not BacklogUnread
        self.now += 300
        self.run_job()                                            # down 1 of 2: not read
        self.now += 300
        with self.assertRaises(KeyError):
            self.run_job()

    def test_an_autostart_off_entry_reads_nothing_and_keeps_no_baseline(self):
        self.held_run()
        self.sessions['kit'] = dict(ENTRY, autostart=False)
        self.write('sessions.json', self.sessions)
        self.offer = {'EX-A'}
        for _ in range(3):
            self.now += 300
            self.run_job()
        self.offer = {'EX-A', 'EX-B'}
        self.now += 300
        result = self.run_job()
        self.assertEqual((self.reads, self.calls), ([], []))
        self.assertNotIn('held', self.records().get('kit', {}))
        self.assertIsInstance(result, jobs.Skip)                  # down_entries leaves it out

    def test_the_cheap_holds_come_before_the_read(self):
        self.held_run()
        self.now += 300
        self.run_job()                                            # down 1 of 2
        self.assertEqual(self.reads, [])
        rec = self.records()
        rec['kit']['starts'] = [self.now - 60]                    # in its backoff
        self.write(C.STATE_FILE, rec)
        self.now += 300
        self.run_job()
        self.assertEqual(self.reads, [])
        self.assertNotIn('held', self.records()['kit'])

    def test_a_new_stop_with_no_sighting_between_resets_the_baseline(self):
        self.held_run()
        for _ in range(2):
            self.now += 300
            self.run_job()                                        # baseline: nothing offered
        first = self.records()['kit']['held']['stop_at']
        self.offer = {'EX-B'}
        self.stop()                                               # stopped again, never seen up
        self.now += 300
        self.run_job()
        held = self.records()['kit']['held']
        self.assertNotEqual(held['stop_at'], first)
        self.assertEqual(held['ids'], ['EX-B'])                   # the new stop's baseline
        self.assertEqual(self.calls, [])
        self.now += 300
        self.run_job()
        self.assertEqual(self.calls, [])                          # EX-B was there at the new stop

    def test_the_backlog_is_read_only_for_a_deliberate_stop(self):
        self.live = ['kit-coord-a1']
        self.run_job()                                            # running
        self.live = []
        for _ in range(2):
            self.now += 300
            self.run_job()                                        # a crash: restarted, no read
        self.assertEqual(self.reads, [])
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('--prompt', self.calls[0])               # a plain restart keeps today's argv


def item(kind, doc_id, at, sender='kit', **more):
    return dict({'kind': kind, 'id': doc_id, 'from': sender, 'at': iso(at), 'route': sender}, **more)


class OwnerParts(unittest.TestCase):
    def test_owner_wake_names_answers_and_decisions_newer_than_since(self):
        w = C.owner_wake('kit', [item('question', 'Q-2', NOW + 5), item('approval', 'A-3', NOW + 9),
                                 item('question', 'Q-1', NOW - 5)], NOW)
        self.assertEqual(w.ids, ['Q-2', 'A-3'])
        self.assertEqual(w.reason, 'an owner answer is pending for it: Q-2; '
                                   'an approval it asked for is decided: A-3')
        self.assertEqual(w.then, 'run hub pending --for kit')

    def test_nothing_newer_or_a_bad_stamp_wakes_nothing(self):
        self.assertIsNone(C.owner_wake('kit', [item('question', 'Q-1', NOW - 5)], NOW))
        self.assertIsNone(C.owner_wake('kit', [{'kind': 'question', 'id': 'Q-1', 'at': 'x'}], NOW))
        self.assertIsNone(C.owner_wake('kit', [], NOW))

    def test_since_is_the_session_start_else_the_stop_less_a_stamp(self):
        self.assertEqual(C.owner_since({'started_at': NOW - 50}, NOW), NOW - 50)
        self.assertEqual(C.owner_since({}, NOW), NOW - C.STAMP_S)
        self.assertEqual(C.owner_since({'started_at': True}, NOW), NOW - C.STAMP_S)

    def test_a_records_wake_prompt_is_unchanged(self):
        w = C.backlog_wake({'EX-B'}, [])
        self.assertEqual(C.wake_prompt(w, 'x.md'), 'Started by the hub: its repo has new startable '
                         'records: EX-B. Read your state note (x.md), then run backlog next.')

    def test_owner_items_keeps_answers_and_approvals_for_the_key(self):
        got = C.owner_items(lambda key: [item('question', 'Q-1', NOW), item('approval', 'A-1', NOW),
                                         item('todo', 'T-1', NOW), item('question', 'Q-2', NOW, 'pm')],
                            'kit')
        self.assertEqual([i['id'] for i in got], ['Q-1', 'A-1'])

    def test_owner_items_unread(self):
        from kitlib.hubsite.store import HubError

        def closed(key):
            raise HubError('store', 'closed')
        with self.assertRaises(C.OwnerUnread):
            C.owner_items(closed, 'kit')
        with self.assertRaises(C.OwnerUnread):
            C.owner_items(None, 'kit')


class OwnerJob(JobBase):
    """The job with its pending read replaced: `self.items`, or an exception; processes carry a
    Created time, `self.created` (None: none)."""

    def setUp(self):
        self.items = []
        self.pend_reads = []
        self.offer = set()
        self.reads = []
        self.created = NOW - 3600
        super().setUp()

    def new_job(self):
        return C.RestartJob(self.settings, procs=self.procs, child=self.child, clock=lambda: self.now,
                            kit=self.tmp, uptime=lambda: self.uptime, detached=self.detached,
                            backlog=self.backlog, pending=self.pending)

    def procs(self):
        rows = [proc(n) for n in self.live]
        if self.created is not None:
            for r in rows:
                r['Created'] = iso(self.created)
        return rows

    def backlog(self, key, entry):
        self.reads.append(key)
        return set(self.offer)

    def pending(self, key, entry):
        self.pend_reads.append(key)
        if isinstance(self.items, Exception):
            raise self.items
        return list(self.items)

    stop = WakeJob.stop
    held_run = WakeJob.held_run

    def down(self, runs=2):
        for _ in range(runs):
            self.now += 300
            result = self.run_job()
        return result

    def test_an_answer_that_came_in_while_it_wound_down_wakes_it(self):
        self.held_run()                                           # last seen NOW, stopped NOW + 60
        self.items = [item('question', 'Q-7', NOW + 30)]          # between the sighting and the stop
        note = self.down()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][-2:], ['--prompt', 'Started by the hub: an owner answer is '
                         'pending for it: Q-7. Read your state note (x.md), then run hub pending '
                         '--for kit.'])
        self.assertIn('(woken: an owner answer is pending for it: Q-7)', note)
        self.assertEqual(self.reads, [])                          # the cheaper read decided
        self.assertEqual(self.ctx.events, [('restarted-unchecked', 'kit:a1')])

    def test_a_decided_approval_and_a_class_question_wake_it(self):
        self.held_run()
        self.items = [item('approval', 'A-4', NOW + 100),
                      item('question', 'Q-8', NOW + 100, **{'class': 'c'})]
        self.down()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('an owner answer is pending for it: Q-8; an approval it asked for is decided: A-4',
                      self.calls[0][-1])

    def test_an_answer_older_than_the_session_start_does_not_wake_it(self):
        self.held_run()
        self.items = [item('question', 'Q-1', NOW - 7200)]        # pending before it started
        self.down(3)
        self.assertEqual(self.calls, [])
        self.assertTrue(self.reads)                               # the records flow still ran

    def test_without_a_start_time_the_stop_is_the_threshold(self):
        self.created = None
        self.held_run()
        self.assertNotIn('started_at', self.records()['kit'])
        self.items = [item('question', 'Q-1', NOW + 30)]          # before the stop at NOW + 60
        self.down()
        self.assertEqual(self.calls, [])
        self.items.append(item('question', 'Q-2', NOW + 90))
        self.down(1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn('Q-2', self.calls[0][-1])
        self.assertNotIn('Q-1', self.calls[0][-1])

    def test_started_at_is_the_process_creation_time(self):
        self.live = ['kit-coord-a1']
        self.run_job()
        self.assertEqual(self.records()['kit']['started_at'], NOW - 3600)

    def test_the_pending_read_waits_for_the_cheap_holds(self):
        self.held_run()
        self.items = [item('question', 'Q-7', NOW + 30)]
        self.now += 300
        self.run_job()                                            # down 1 of 2
        self.assertEqual(self.pend_reads, [])
        rec = self.records()
        rec['kit']['starts'] = [self.now - 60]                    # in its backoff
        self.write(C.STATE_FILE, rec)
        self.now += 300
        self.run_job()
        self.assertEqual((self.pend_reads, self.calls), ([], []))

    def test_a_crash_and_autostart_off_read_nothing(self):
        self.live = ['kit-coord-a1']
        self.run_job()
        self.live = []
        self.items = [item('question', 'Q-7', NOW + 30)]
        self.down()                                               # no stop.log line: a crash
        self.assertEqual(self.pend_reads, [])
        self.assertNotIn('--prompt', self.calls[0])
        self.sessions['kit'] = dict(ENTRY, autostart=False)
        self.write('sessions.json', self.sessions)
        self.stop()
        self.down(3)
        self.assertEqual(self.pend_reads, [])

    def test_a_woken_coordinator_that_stops_again_is_not_woken_for_the_same_answer(self):
        self.held_run()
        self.items = [item('question', 'Q-7', NOW + 30)]
        self.down()
        self.assertEqual(len(self.calls), 1)
        self.live = ['kit-coord-a1']
        self.created = self.now + 10                              # the new session starts after Q-7
        self.run_job()
        self.live = []
        self.stop()                                               # it found nothing to do, stops
        for _ in range(4):
            self.now += 3600
            self.run_job()
        self.assertEqual(len(self.calls), 1)

    def test_pending_unread_is_a_note_and_the_records_wake_still_runs(self):
        self.held_run()
        self.items = C.OwnerUnread('the hub gives the job no pending read')
        self.down(1)
        self.offer = {'EX-A'}
        self.now += 300
        result = self.run_job()
        self.assertIsInstance(result, jobs.Skip)
        self.assertIn('kit: owner answers not read, so not woken for them', result.note)
        self.assertEqual(self.records()['kit']['held']['ids'], ['EX-A'])   # the baseline was kept

    def test_make_reads_sources_pending_at_run_time(self):
        class Src(object):
            home = None
        src = Src()
        with mock.patch('kitlib.launcher.settings.load', return_value=self.settings):
            job = C.make(src)
        with self.assertRaises(C.OwnerUnread):
            job.run.pending('kit', ENTRY)                         # the hub has not set it yet
        src.pending = lambda key: [item('question', 'Q-1', NOW), item('todo', 'T-1', NOW)]
        self.assertEqual([i['id'] for i in job.run.pending('kit', ENTRY)], ['Q-1'])


if __name__ == '__main__':
    unittest.main()
