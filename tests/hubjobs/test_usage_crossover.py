"""kitlib.hubjobs.usage_crossover: the decision, the target, and the job's states around them."""
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
from kitlib.hubjobs import usage_crossover as U
from kitlib.hubsite import jobs, push
from kitlib.launcher import settings as lsettings
from kitlib.launcher.settings import LaunchError

NOW = 1_800_000_000.0
DAY = 86400
ENTRY = {'repo': 'kit', 'agent': True, 'stem': 'kit-coord', 'state': 'x.md', 'account': 1,
         'name': 'kit-coord-a1'}
MOVED = dict(ENTRY, account=2, name='kit-coord-a2')
CREATED = '2027-01-01T00:00:00'
OLD = U.Live('kit-coord-a1', 11, CREATED)
NEW = U.Live('kit-coord-a2', 22, CREATED)
STALL = ('1', NOW + 2 * DAY)


def iso(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def reading(week=20, five=10, week_in=2 * DAY, five_in=3600, age=60):
    return {'updated_at': iso(NOW - age),
            'five_hour': {'used_percentage': five, 'resets_at': iso(NOW + five_in)},
            'seven_day': {'used_percentage': week, 'resets_at': iso(NOW + week_in)}}


def on(*args, **kwargs):
    return C.AccountState(True, reading(*args, **kwargs))


HOT = on(99)
ACCOUNTS = {'1': HOT, '2': on(20)}


def crossing(phase, **fields):
    c = {'phase': phase, 'phase_at': NOW - 60, 'asked_at': NOW - 120, 'account': '1',
         'target': '2', 'old_name': OLD.name, 'old_pid': OLD.pid, 'old_created': OLD.created,
         'entry_name': OLD.name, 'high_c': 5, 'delivered': True}
    if phase in (U.STARTING, U.STARTED, U.STOPPING):
        c['new_name'] = NEW.name
    c.update(fields)
    return c


def decide(entry=ENTRY, cross=None, live=(OLD,), stall=None, quiet=None, accounts=ACCOUNTS,
           newest=None, starts=None, running=None, landed=None, mode='normal', now=NOW,
           note_at=None, cleared=None, asks=None):
    return U.decide('kit', entry, cross, list(live), stall, quiet, accounts, newest, starts or {},
                    {'1': 1} if running is None else running, landed or {}, mode, now,
                    note_at=note_at, cleared=cleared, asks=asks)


def record(number, kind):
    return {'id': 'C-%03d' % number, 'kind': kind, 'state_path': 'Z:/note-%d.md' % number}


class Trigger(unittest.TestCase):
    def test_a_fresh_reading_at_99_of_the_week_asks_for_a_checkpoint(self):
        d = decide()
        self.assertEqual((d.action, d.target, d.word, d.detail), (U.ASK, '2', 'crossing', 'kit:a2'))

    def test_98_of_the_week_does_nothing(self):
        self.assertEqual(decide(accounts={'1': on(98), '2': on(20)}).action, U.HOLD)

    def test_the_five_hour_window_is_ignored(self):
        self.assertEqual(decide(accounts={'1': on(20, five=100), '2': on(20)}).action, U.HOLD)

    def test_a_stale_reading_at_99_with_no_stall_read_holds(self):
        d = decide(accounts={'1': on(99, age=3660 + 60), '2': on(20)})
        self.assertEqual(d.action, U.HOLD)
        self.assertIn('stale', d.note)

    def test_a_reading_an_hour_old_still_counts_and_a_minute_more_does_not(self):
        self.assertEqual(decide(accounts={'1': on(99, age=3659), '2': on(20)}).action, U.ASK)
        self.assertEqual(decide(accounts={'1': on(99, age=3660), '2': on(20)}).action, U.HOLD)

    def test_a_week_that_reset_counts_as_zero(self):
        self.assertEqual(decide(accounts={'1': on(99, week_in=-60), '2': on(20)}).action, U.HOLD)

    def test_a_reading_without_the_week_never_counts(self):
        data = reading(99)
        del data['seven_day']['used_percentage']
        accounts = {'1': C.AccountState(True, data), '2': on(20)}
        self.assertEqual(decide(accounts=accounts).action, U.HOLD)

    def test_a_reset_within_the_hour_is_waited_for(self):
        d = decide(accounts={'1': on(99, week_in=3599), '2': on(20)})
        self.assertEqual(d.action, U.HOLD)
        self.assertIn('resets within', d.note)
        self.assertEqual(decide(accounts={'1': on(99, week_in=3601), '2': on(20)}).action, U.ASK)

    def test_no_entry_detail_or_process_holds(self):
        self.assertEqual(decide(entry=dict(ENTRY, stem='')).action, U.HOLD)
        self.assertEqual(decide(entry=dict(ENTRY, state='')).action, U.HOLD)
        self.assertEqual(decide(live=()).action, U.HOLD)

    def test_several_processes_of_the_stem_hold(self):
        d = decide(live=(OLD, NEW))
        self.assertEqual(d.action, U.HOLD)
        self.assertIn('several', d.note)

    def test_no_room_holds_and_pushes(self):
        d = decide(accounts={'1': HOT, '2': on(98)})
        self.assertEqual((d.action, d.word, d.detail), (U.HOLD, 'no-room', 'kit'))

    def test_a_cap_on_the_key_or_the_machine_holds_before_the_child(self):
        three = {'kit': [NOW - 60, NOW - 120, NOW - 180]}
        self.assertEqual(decide(starts=three).word, 'cross-failed')
        six = {'a': [NOW - 60] * 3, 'b': [NOW - 60] * 3}
        d = decide(starts=six)
        self.assertEqual((d.action, d.word), (U.HOLD, 'cross-failed'))
        self.assertIn('machine', d.note)

    def test_starts_older_than_the_window_do_not_count(self):
        old = {'kit': [NOW - 2 * DAY] * 3}
        self.assertEqual(decide(starts=old).action, U.ASK)

    def test_notify_only_says_what_it_would_do(self):
        d = decide(mode='notify')
        self.assertEqual((d.action, d.target, d.word), (U.WOULD, '2', 'would-cross'))
        d = decide(mode='notify', stall=STALL, quiet=120, accounts={'1': on(50), '2': on(20)})
        self.assertEqual(d.action, U.WOULD)

    def test_a_crossing_cleared_this_week_on_the_account_is_not_asked_again(self):
        week_reset = NOW + 2 * DAY
        d = decide(cleared={'account': '1', 'reset': week_reset + 30})
        self.assertEqual((d.action, d.word), (U.HOLD, None))
        self.assertIn('cleared', d.note)
        d = decide(cleared={'account': '1', 'reset': week_reset}, mode='full', stall=STALL, quiet=600)
        self.assertEqual((d.action, d.word), (U.HOLD, None))        # full: not stopped, no push
        self.assertEqual(decide(cleared={'account': '1', 'reset': week_reset - 7 * DAY}).action, U.ASK)
        self.assertEqual(decide(cleared={'account': '3', 'reset': week_reset}).action, U.ASK)

    def test_a_cleared_key_that_stalls_on_normal_is_still_pushed_stalled(self):
        d = decide(cleared={'account': '1', 'reset': NOW + 2 * DAY}, stall=STALL, quiet=600)
        self.assertEqual((d.action, d.word, d.detail), (U.HOLD, 'stalled', 'kit'))

    def test_asks_have_a_cap_per_key(self):
        d = decide(asks=[NOW - 60, NOW - 120, NOW - 180])
        self.assertEqual((d.action, d.word, d.mark), (U.HOLD, 'cross-failed', 'ask-cap'))
        self.assertEqual(decide(asks=[NOW - 60, NOW - 120]).action, U.ASK)
        self.assertEqual(decide(asks=[NOW - 2 * DAY] * 3).action, U.ASK)


class Stall(unittest.TestCase):
    QUIET = {'1': on(50), '2': on(20)}

    def test_full_stops_a_stalled_session_first(self):
        d = decide(stall=STALL, quiet=60, accounts=self.QUIET, mode='full')
        self.assertEqual((d.action, d.target, d.word), (U.STOP_STALLED, '2', 'stalled'))

    def test_normal_holds_and_tells_the_pm(self):
        d = decide(stall=STALL, quiet=60, accounts=self.QUIET)
        self.assertEqual((d.action, d.word), (U.HOLD, 'stalled'))

    def test_normal_with_a_fresh_reading_at_99_still_never_asks_a_stalled_session(self):
        d = decide(stall=STALL, quiet=600, accounts=ACCOUNTS)       # account 1 at 99%, fresh
        self.assertEqual((d.action, d.word, d.detail), (U.HOLD, 'stalled', 'kit'))

    def test_activity_under_a_minute_old_is_no_stall(self):
        d = decide(stall=STALL, quiet=59, accounts=self.QUIET, mode='full')
        self.assertEqual(d.action, U.HOLD)
        self.assertIn('active', d.note)

    def test_an_unknown_quiet_time_is_no_stall(self):
        self.assertEqual(decide(stall=STALL, quiet=None, accounts=self.QUIET, mode='full').action,
                         U.HOLD)

    def test_a_stall_read_whose_reset_has_passed_is_no_stall(self):
        d = decide(stall=('1', NOW - 1), quiet=600, accounts=self.QUIET, mode='full')
        self.assertEqual(d.action, U.HOLD)

    def test_a_stall_with_the_reset_near_waits_for_it(self):
        d = decide(stall=('1', NOW + 600), quiet=600, accounts=self.QUIET, mode='full')
        self.assertEqual(d.action, U.HOLD)
        self.assertIn('resets within', d.note)

    def test_a_stall_with_no_room_stops_nothing(self):
        d = decide(stall=STALL, quiet=600, accounts={'1': on(50), '2': on(99)}, mode='full')
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-room'))

    def test_the_stall_reads_account_is_the_sessions_even_when_its_name_says_otherwise(self):
        live = U.Live('kit-coord-a2', 11, CREATED)
        d = decide(entry=dict(ENTRY, name='kit-coord-a2', account=2), live=(live,),
                   stall=('1', NOW + DAY), quiet=600, accounts={'1': on(50), '2': on(50), '3': on(20)},
                   mode='full')
        self.assertEqual((d.action, d.target), (U.STOP_STALLED, '3'))


class Target(unittest.TestCase):
    def pick(self, accounts, current=('1',), running=None, landed=None):
        return U.pick_target(accounts, set(current), running or {}, landed or {}, NOW)

    def test_tier_one_is_the_week_that_resets_first(self):
        accounts = {'2': on(80, week_in=3 * DAY), '3': on(10, week_in=5 * DAY), '4': on(90, week_in=DAY)}
        self.assertEqual(self.pick(accounts)[0], '4')

    def test_tier_two_is_the_lowest_week_under_98(self):
        accounts = {'2': on(97), '3': on(95), '4': on(98)}
        self.assertEqual(self.pick(accounts)[0], '3')

    def test_tier_one_wins_over_a_sooner_tier_two(self):
        accounts = {'2': on(96, week_in=DAY), '3': on(94, week_in=5 * DAY)}
        self.assertEqual(self.pick(accounts)[0], '3')

    def test_the_five_hour_window_must_have_room_in_both_tiers(self):
        self.assertIsNone(self.pick({'2': on(20, five=95)}))
        self.assertIsNone(self.pick({'2': on(96, five=95)}))

    def test_a_passed_five_hour_window_has_room(self):
        self.assertEqual(self.pick({'2': on(20, five=100, five_in=-60)})[0], '2')

    def test_98_of_the_week_is_no_target(self):
        self.assertIsNone(self.pick({'2': on(98)}))

    def test_the_current_account_and_a_signed_out_one_are_left_out(self):
        accounts = {'1': on(10), '2': C.AccountState(False, reading(10))}
        self.assertIsNone(self.pick(accounts))

    def test_an_unknown_reading_is_left_out(self):
        self.assertIsNone(self.pick({'2': C.AccountState(True, None)}))

    def test_a_stale_reading_counts_only_with_no_live_session_there(self):
        accounts = {'2': on(20, age=7200)}
        self.assertEqual(self.pick(accounts)[0], '2')
        self.assertIsNone(self.pick(accounts, running={'2': 1}))

    def test_no_second_start_until_a_reading_newer_than_the_last(self):
        accounts = {'2': on(20, age=60)}
        self.assertIsNone(self.pick(accounts, landed={'2': NOW - 30}))
        self.assertIsNone(self.pick(accounts, landed={'2': NOW - 60}))
        self.assertEqual(self.pick(accounts, landed={'2': NOW - 61})[0], '2')

    def test_sessions_jsons_own_account_is_left_out_of_a_trigger(self):
        entry = dict(ENTRY, account=2)      # the name says account 1: handoff would refuse 2
        d = decide(entry=entry, accounts={'1': HOT, '2': on(20), '3': on(20)})
        self.assertEqual(d.target, '3')


class Asked(unittest.TestCase):
    def step(self, *args, **kwargs):
        kwargs.setdefault('cross', crossing(U.ASKED, *args))
        return decide(**kwargs)

    def test_it_waits(self):
        self.assertEqual(self.step().action, U.HOLD)

    def test_a_newer_ready_record_moves_on_with_its_note(self):
        for kind in ('handoff', 'restart'):
            d = self.step(newest=record(6, kind))
            self.assertEqual((d.action, d.phase, d.fields['state_path']),
                             (U.ADVANCE, U.READY, 'Z:/note-6.md'))

    def test_a_record_not_newer_than_the_ask_is_not_its_reply(self):
        self.assertEqual(self.step(newest=record(5, 'handoff')).action, U.HOLD)

    def test_a_checkin_record_is_no_ready_reply(self):
        self.assertEqual(self.step(newest=record(6, 'checkin')).action, U.HOLD)

    def test_ready_to_stop_clears_it(self):
        d = self.step(newest=record(6, 'stop'))
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-cleared'))

    def test_not_delivered_asks_again_with_no_second_push(self):
        d = self.step(cross=crossing(U.ASKED, delivered=False))
        self.assertEqual((d.action, d.word), (U.ASK, None))

    def test_half_an_hour_says_no_ready_once_and_stays_open(self):
        d = self.step(cross=crossing(U.ASKED, phase_at=NOW - U.WAIT_READY_S))
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-ready'))

    def test_a_week_that_reset_clears_it(self):
        d = self.step(accounts={'1': on(99, week_in=-60), '2': on(20)})
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-cleared'))

    def test_the_session_gone_or_replaced_clears_it(self):
        self.assertEqual(self.step(live=()).action, U.CLEAR)
        same_pid_new_process = U.Live(OLD.name, OLD.pid, 'another time')
        self.assertEqual(self.step(live=(same_pid_new_process,)).action, U.CLEAR)

    def test_an_entry_moved_by_someone_else_clears_it(self):
        self.assertEqual(self.step(entry=MOVED).action, U.CLEAR)

    def test_a_stall_on_full_goes_stop_first(self):
        d = self.step(mode='full', stall=STALL, quiet=600)
        self.assertEqual((d.action, d.phase, d.fields), (U.ADVANCE, U.STALL_STOP, {'stalled': True}))

    def test_a_stall_on_normal_is_left_to_the_pm_as_stalled(self):
        d = self.step(stall=STALL, quiet=600)
        self.assertEqual((d.action, d.word, d.detail, d.remember), (U.CLEAR, 'stalled', 'kit', False))

    def test_ready_to_stop_is_remembered_for_the_week(self):
        d = self.step(newest=record(6, 'stop'))
        self.assertEqual(d.fields, {'account': '1', 'reset': NOW + 2 * DAY})

    def test_a_stall_on_full_with_no_room_stops_nothing(self):
        d = self.step(mode='full', stall=STALL, quiet=600, accounts={'1': HOT, '2': on(99)})
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-room'))

    def test_notify_drops_an_asked_crossing(self):
        self.assertEqual(self.step(mode='notify').action, U.CLEAR)


class Ready(unittest.TestCase):
    def step(self, **kwargs):
        fields = kwargs.pop('fields', {})
        kwargs.setdefault('quiet', 600)
        kwargs.setdefault('note_at', NOW - 30)          # after the ask at NOW - 120
        return decide(cross=crossing(U.READY, state_path='Z:/n.md', **fields), **kwargs)

    def test_it_starts_on_the_target_picked_now(self):
        d = self.step(accounts={'1': HOT, '2': on(99), '3': on(20)})
        self.assertEqual((d.action, d.target), (U.START, '3'))

    def test_a_state_note_not_written_since_the_ask_never_starts(self):
        for note_at in (None, NOW - 120, NOW - 3 * DAY):
            d = self.step(note_at=note_at)
            self.assertEqual((d.action, d.word), (U.HOLD, None), note_at)
            self.assertIn('not written since the ask', d.note)

    def test_a_transcript_written_under_a_minute_ago_never_starts(self):
        for quiet in (None, 0, U.QUIET_S - 1):
            d = self.step(quiet=quiet)
            self.assertEqual(d.action, U.HOLD, quiet)
            self.assertIn('transcript', d.note)
        self.assertEqual(self.step(quiet=U.QUIET_S).action, U.START)

    def test_half_an_hour_unsettled_says_no_ready(self):
        d = self.step(note_at=None, fields={'phase_at': NOW - U.WAIT_READY_S})
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-ready'))

    def test_its_own_start_moved_the_entry_with_no_successor_fails_it(self):
        d = self.step(entry=MOVED, fields={'new_name': NEW.name})
        self.assertEqual((d.action, d.word, d.remember), (U.CLEAR, 'cross-failed', True))
        self.assertIn('which is not running; the old session stays', d.note)

    def test_a_successor_up_is_never_started_again(self):
        d = self.step(live=(OLD, U.Live('kit-coord-a2', 22, CREATED)),
                      fields={'new_name': NEW.name})
        self.assertEqual((d.action, d.phase), (U.ADVANCE, U.STARTING))

    def test_the_old_session_gone_clears_it(self):
        self.assertEqual(self.step(live=()).action, U.CLEAR)

    def test_an_entry_moved_by_someone_else_clears_it(self):
        self.assertEqual(self.step(entry=MOVED).action, U.CLEAR)

    def test_the_cap_fails_it(self):
        d = self.step(starts={'kit': [NOW - 60] * 3})
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-failed'))

    def test_backoff_holds(self):
        self.assertEqual(self.step(fields={'retry_at': NOW + 1}).action, U.HOLD)
        self.assertEqual(self.step(fields={'retry_at': NOW}).action, U.START)

    def test_no_room_holds_with_nothing_stopped(self):
        d = self.step(accounts={'1': HOT, '2': on(99)})
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-room'))

    def test_a_stalled_session_already_stopped_starts_with_nothing_live(self):
        self.assertEqual(self.step(live=(), fields={'stalled': True}).action, U.START)

    def test_a_stalled_session_started_again_by_someone_clears_it(self):
        self.assertEqual(self.step(fields={'stalled': True}).action, U.CLEAR)


class Starting(unittest.TestCase):
    def test_up_and_moved_is_started(self):
        d = decide(entry=MOVED, cross=crossing(U.STARTING), live=(OLD, NEW))
        self.assertEqual((d.action, d.phase), (U.ADVANCE, U.STARTED))

    def test_up_and_not_moved_finishes_the_write_and_never_runs_handoff(self):
        self.assertEqual(decide(cross=crossing(U.STARTING), live=(OLD, NEW)).action, U.FINISH)

    def test_a_short_wait_holds(self):
        self.assertEqual(decide(cross=crossing(U.STARTING)).action, U.HOLD)

    def test_no_successor_in_five_minutes_backs_off_to_ready(self):
        d = decide(cross=crossing(U.STARTING, phase_at=NOW - U.START_WAIT_S))
        self.assertEqual((d.action, d.phase, d.fields['fails']), (U.ADVANCE, U.READY, 1))
        self.assertEqual(d.fields['retry_at'], NOW + U.BACKOFF_S[0])
        d = decide(cross=crossing(U.STARTING, phase_at=NOW - U.START_WAIT_S, fails=9))
        self.assertEqual(d.fields['retry_at'], NOW + U.BACKOFF_S[-1])


class Started(unittest.TestCase):
    def step(self, **kwargs):
        cross = kwargs.pop('cross', crossing(U.STARTED))
        kwargs.setdefault('live', (OLD, NEW))
        return decide(entry=MOVED, cross=cross, **kwargs)

    def test_a_newer_checkin_record_moves_to_the_stop(self):
        d = self.step(newest=record(6, 'checkin'))
        self.assertEqual((d.action, d.phase), (U.ADVANCE, U.STOPPING))

    def test_an_older_or_other_record_does_not(self):
        self.assertEqual(self.step(newest=record(5, 'checkin')).action, U.HOLD)
        self.assertEqual(self.step(newest=record(6, 'handoff')).action, U.HOLD)

    def test_half_an_hour_with_no_checkin_never_stops_anything(self):
        d = self.step(cross=crossing(U.STARTED, phase_at=NOW - 10 * U.WAIT_CHECKIN_S))
        self.assertEqual((d.action, d.word), (U.HOLD, 'no-checkin'))

    def test_the_old_session_gone_is_done(self):
        d = self.step(live=(NEW,))
        self.assertEqual((d.action, d.word, d.detail), (U.CLEAR, 'crossed', NEW.name))

    def test_the_successor_gone_fails_it_and_stops_nothing(self):
        d = self.step(live=(OLD,))
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-failed'))

    def test_a_stall_of_the_old_session_on_full_stops_it(self):
        d = self.step(mode='full', stall=STALL, quiet=600)
        self.assertEqual((d.action, d.phase), (U.ADVANCE, U.STALL_STOP))


class Stopping(unittest.TestCase):
    def step(self, live):
        return decide(entry=MOVED, cross=crossing(U.STOPPING), live=live)

    def test_it_stops_the_old_session(self):
        self.assertEqual(self.step((OLD, NEW)).action, U.STOP_OLD)

    def test_a_successor_gone_stops_nothing(self):
        d = self.step((OLD,))
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-failed'))

    def test_the_old_session_gone_is_done(self):
        self.assertEqual(self.step((NEW,)).word, 'crossed')

    def test_a_new_process_under_the_old_name_is_never_stopped(self):
        d = self.step((U.Live(OLD.name, 99, CREATED), NEW))
        self.assertEqual((d.action, d.word), (U.CLEAR, 'crossed'))

    def test_a_refused_stop_waits_out_its_backoff_then_gives_up(self):
        def step(**fields):
            return decide(entry=MOVED, cross=crossing(U.STOPPING, **fields), live=(OLD, NEW))
        self.assertEqual(step(stop_tries=1, stop_retry_at=NOW + 1).action, U.HOLD)
        self.assertEqual(step(stop_tries=1, stop_retry_at=NOW).action, U.STOP_OLD)
        d = step(stop_tries=U.STOP_TRIES, stop_retry_at=NOW - 1)
        self.assertEqual((d.action, d.word, d.remember), (U.CLEAR, 'cross-failed', True))


class StallStop(unittest.TestCase):
    def step(self, **kwargs):
        kwargs.setdefault('mode', 'full')
        return decide(cross=crossing(U.STALL_STOP, stalled=True), **kwargs)

    def test_it_stops_a_session_still_stalled(self):
        self.assertEqual(self.step(stall=STALL, quiet=600).action, U.STOP_STALLED)

    def test_a_session_no_longer_stalled_is_not_stopped(self):
        d = self.step()
        self.assertEqual((d.action, d.word), (U.CLEAR, 'cross-cleared'))

    def test_a_mode_turned_down_stops_nothing(self):
        d = self.step(mode='normal', stall=STALL, quiet=600)
        self.assertEqual(d.action, U.CLEAR)

    def test_the_session_gone_goes_on_to_the_start_on_its_own_note(self):
        d = self.step(live=())
        self.assertEqual((d.action, d.phase, d.fields), (U.ADVANCE, U.READY, {'stalled': True}))

    def test_a_successor_up_goes_back_to_started(self):
        d = decide(entry=MOVED, cross=crossing(U.STALL_STOP, new_name=NEW.name), live=(NEW,),
                   mode='full')
        self.assertEqual((d.action, d.phase), (U.ADVANCE, U.STARTED))


class Notify(unittest.TestCase):
    def test_a_crossing_past_the_start_is_left_as_it_is(self):
        for phase in (U.STARTING, U.STARTED, U.STOPPING, U.STALL_STOP):
            d = decide(cross=crossing(phase), mode='notify')
            self.assertEqual(d.action, U.HOLD, phase)

    def test_an_entry_gone_clears_any_crossing(self):
        self.assertEqual(decide(entry=None, cross=crossing(U.STARTED)).action, U.CLEAR)


class Words(unittest.TestCase):
    def test_every_word_is_owed_once_per_detail(self):
        for word in ('crossing', 'would-cross', 'crossed', 'cross-cleared', 'cross-failed', 'stalled',
                     'no-ready', 'no-checkin', 'no-room', 'pm-limit', 'unmanaged'):
            self.assertIn(word, push.JOB_EACH)


class Ctx(object):
    def __init__(self):
        self.stop = threading.Event()
        self.events = []

    def event(self, word, detail=None):
        self.events.append((word, detail))


class Host(object):
    """The watcher readers: `records` the registry rows, `written` {name: transcript mtime}."""

    def __init__(self):
        self.records = []
        self.written = {}

    def rc_records(self, dirs):
        return self.records

    def newest_finder(self):
        return self.written.get


class JobBase(unittest.TestCase):
    """The job over a temporary state folder; processes, children, readings and clock faked."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, 'state')
        self.acct = {n: os.path.join(self.tmp, 'a' + n) for n in ('1', '2', '3')}
        for d in [self.state] + list(self.acct.values()):
            os.makedirs(d)
        for d in self.acct.values():
            open(os.path.join(d, '.credentials.json'), 'w').close()
        self.write('sessions.json', {'_note': 'x', 'kit': dict(ENTRY)})
        self.mode = 'normal'
        self.now = NOW
        self.live = [OLD]
        self.usage = {'1': reading(99), '2': reading(20), '3': reading(20, week_in=3 * DAY)}
        self.newest = {}
        self.calls = []
        self.code = 0
        self.sent = []
        self.delivers = True
        self.host = Host()
        self.saved_at_child = None
        for target, name, value in (
                (U.lookup, 'launched', lambda rows: rows),
                (U.A, 'newest_usage', lambda s, n, by: (None, self.usage.get(str(n)))),
                (U.pmwatch, 'account_folders', lambda s: {'f1': '1', 'f2': '2', 'f3': '3'}),
                (U.pipes, 'registry_dirs', lambda s: [])):
            p = mock.patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.job = self.new_job()

    def load(self):
        return lsettings.Settings({'state_dir': self.state, 'usage_crossover': self.mode,
                                   'accounts': {n: [d] for n, d in self.acct.items()}}, home=self.tmp)

    def new_job(self):
        return U.CrossoverJob(self.load, self.deliver, lambda: self.newest, self.host,
                              procs=self.procs, child=self.child, clock=lambda: self.now,
                              kit=self.tmp)

    def procs(self):
        return [{'Name': 'claude.exe', 'ProcessId': l.pid, 'Created': l.created,
                 'CommandLine': 'claude.exe --remote-control %s -n %s' % (l.name, l.name)}
                for l in self.live]

    def deliver(self, name, text):
        self.sent.append((name, text))
        return self.delivers

    def child(self, argv, cwd):
        self.calls.append(argv[2:])
        file = os.path.join(self.state, U.STATE_FILE)
        self.saved_at_child = self.doc() if os.path.exists(file) else None
        if isinstance(self.code, Exception):
            raise self.code
        return self.code, ['said']

    def write(self, name, data):
        with open(os.path.join(self.state, name), 'w', encoding='utf-8') as f:
            json.dump(data, f)

    def read(self, name):
        with open(os.path.join(self.state, name), encoding='utf-8') as f:
            return json.load(f)

    def doc(self):
        return self.read(U.STATE_FILE)

    def crossing(self, key='kit'):
        return self.doc()['crossings'].get(key)

    def put(self, cross, key='kit', **parts):
        self.write(U.STATE_FILE, dict({'crossings': {key: cross}}, **parts))

    def stall(self, name, pid, account, reset=NOW + 2 * DAY, written=NOW - 600):
        folder = 'f' + account
        self.host.records.append({'name': name, 'pid': pid,
                                  'weekly_stop': lambda folders: (folder, reset)})
        self.host.written[name] = written

    def note(self, number, written, kind='handoff'):
        """A hand-off record whose state note is a real file last written at `written`."""
        path = os.path.join(self.tmp, 'note-%d.md' % number)
        with open(path, 'w') as f:
            f.write('note')
        os.utime(path, (written, written))
        return {'id': 'C-%03d' % number, 'kind': kind, 'state_path': path}

    def ready(self, **fields):
        """A crossing in ready whose session has settled: its note written after the ask, its
        transcript quiet ten minutes."""
        rec = self.note(6, NOW - 30)
        self.host.written[OLD.name] = NOW - 600
        self.put(crossing(U.READY, state_path=rec['state_path'], **fields))
        return rec

    def run_job(self, ctx=None):
        self.ctx = ctx or Ctx()
        return self.job(self.ctx)

    def log_text(self):
        with open(os.path.join(self.state, U.LOG), encoding='utf-8') as f:
            return f.read()


class Job(JobBase):
    def test_the_ask_writes_the_crossing_first_and_notes_the_newest_record(self):
        self.newest = {'kit': record(7, 'handoff')}
        seen = []
        real = self.deliver
        self.job.deliver = lambda name, text: (seen.append(self.crossing()), real(name, text))[1]
        self.assertEqual(self.run_job(), self.ctx_note())
        self.assertEqual(seen[0]['phase'], U.ASKED)
        self.assertEqual(self.sent[0][0], OLD.name)
        self.assertIn('account 2', self.sent[0][1])
        self.assertIn('hub_handoff_ready', self.sent[0][1])
        self.assertIn('session-handoff', self.sent[0][1])
        c = self.crossing()
        self.assertEqual((c['high_c'], c['delivered'], c['old_pid'], c['target']), (7, True, 11, '2'))
        self.assertEqual(self.ctx.events, [('crossing', 'kit:a2')])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.doc()['asks'], {'kit': [NOW]})

    def ctx_note(self):
        return 'kit: asked to checkpoint for account 2; kit: account 1 99% of its week: to account 2 ' \
               '(under 95%, its week resets first)'

    def test_a_second_run_does_not_ask_again(self):
        self.run_job()
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.ctx.events, [])

    def test_a_message_not_delivered_is_asked_again_with_one_push(self):
        self.delivers = False
        self.run_job()
        self.assertFalse(self.crossing()['delivered'])
        self.delivers = True
        self.run_job()
        self.assertTrue(self.crossing()['delivered'])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.ctx.events, [])

    def test_the_whole_move_ends_with_the_old_session_stopped_after_the_checkin(self):
        self.run_job()
        self.newest = {'kit': self.note(1, NOW + 100)}
        self.host.written[OLD.name] = NOW + 120             # quiet three minutes at the next run
        self.now += 300
        self.run_job()
        self.assertEqual(self.calls, [['handoff', '--session', 'kit', '--account', '2', '--state',
                                       self.newest['kit']['state_path']]])
        self.assertEqual(self.saved_at_child['crossings']['kit']['phase'], U.STARTING)
        self.assertEqual(self.saved_at_child['starts'], {'kit': [NOW + 300]})
        self.assertEqual(self.saved_at_child['landed'], {'2': NOW + 300})
        self.assertEqual(self.crossing()['high_c'], 1)
        # launch.py handoff did it: the successor runs and sessions.json moved
        self.live = [OLD, NEW]
        self.write('sessions.json', {'kit': MOVED})
        self.now += 300
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.crossing()['phase'], U.STARTED)
        self.now += 300
        self.newest = {'kit': record(2, 'checkin')}
        self.run_job()
        self.assertEqual(self.calls[-1], ['stop', '--name', OLD.name, '--successor', NEW.name,
                                          '--pid', '11'])
        self.assertEqual(self.saved_at_child['crossings']['kit']['phase'], U.STOPPING)
        self.live = [NEW]
        self.now += 300
        self.run_job()
        self.assertEqual(self.ctx.events, [('crossed', NEW.name)])
        self.assertIsNone(self.crossing())

    def test_a_ready_record_on_a_note_older_than_the_ask_never_starts(self):
        self.run_job()
        self.newest = {'kit': self.note(1, NOW - 3600)}     # an old note posted again
        self.host.written[OLD.name] = NOW - 600
        self.now += 300
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.crossing()['phase'], U.READY)
        self.assertIn('not written since the ask', self.log_text())

    def test_a_ready_record_waits_for_the_transcript_to_go_quiet(self):
        self.run_job()
        self.newest = {'kit': self.note(1, NOW + 250)}
        self.host.written[OLD.name] = NOW + 290              # still writing 10 s before the run
        self.now += 300
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.calls, [])
        self.now += 300
        self.run_job()
        self.assertEqual(self.calls[0][:5], ['handoff', '--session', 'kit', '--account', '2'])

    def test_ready_to_stop_clears_and_is_not_asked_again_on_the_next_runs(self):
        self.run_job()
        self.newest = {'kit': record(1, 'stop')}
        self.now += 300
        self.run_job()
        self.assertEqual(self.ctx.events, [('cross-cleared', 'kit')])
        for _ in range(3):
            self.now += 300
            self.usage['1'] = reading(99, age=60 - (self.now - NOW))
            self.run_job()
            self.assertEqual(self.ctx.events, [])
        self.assertEqual((len(self.sent), self.calls), (1, []))
        self.assertEqual(self.doc()['cleared'], {'kit': {'account': '1', 'reset': NOW + 2 * DAY}})

    def test_an_idle_predecessor_whose_successor_died_is_not_asked_or_started_again(self):
        self.put(crossing(U.STARTED, high_c=1))
        self.write('sessions.json', {'kit': MOVED})
        self.live = [OLD]
        self.run_job()
        self.assertEqual(self.ctx.events, [('cross-failed', 'kit')])
        self.now += 300
        self.usage['1'] = reading(99, age=-240)
        self.run_job()
        self.assertEqual((self.sent, self.calls, self.ctx.events), ([], [], []))

    def test_a_cleared_mark_lapses_when_its_week_resets(self):
        self.write(U.STATE_FILE, {'cleared': {'kit': {'account': '1', 'reset': NOW - 1}}})
        self.run_job()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.doc()['cleared'], {})

    def test_a_refused_stop_backs_off_then_gives_up_with_the_old_session_left(self):
        self.put(crossing(U.STOPPING))
        self.write('sessions.json', {'kit': MOVED})
        self.live = [OLD, NEW]
        self.code = 2
        for wait in U.BACKOFF_S:
            with self.assertRaises(U.StepFailed):
                self.run_job()
            calls = len(self.calls)
            self.now += 60
            self.assertIsInstance(self.run_job(), jobs.Skip)
            self.assertEqual(len(self.calls), calls)
            self.now += wait - 60
        self.run_job()
        self.assertEqual(len(self.calls), U.STOP_TRIES)
        self.assertEqual(self.ctx.events, [('cross-failed', 'kit')])
        self.assertIsNone(self.crossing())

    def test_the_started_state_is_read_back_after_a_hub_restart(self):
        self.put(crossing(U.STARTED, high_c=1))
        self.write('sessions.json', {'kit': MOVED})
        self.live = [OLD, NEW]
        self.newest = {'kit': record(2, 'checkin')}
        self.job = self.new_job()
        self.run_job()
        self.assertEqual(self.calls[-1][:3], ['stop', '--name', OLD.name])

    def test_a_handoff_that_died_before_its_write_is_finished_never_run_again(self):
        self.put(crossing(U.STARTING))
        self.write('coordinators.json', {'kit': 1, 'pm_account': 3})
        self.live = [OLD, NEW]
        self.run_job()
        self.assertEqual(self.calls, [])
        self.assertEqual(self.read('sessions.json')['kit']['name'], NEW.name)
        self.assertEqual(self.read('sessions.json')['kit']['account'], 2)
        self.assertEqual(self.read('coordinators.json'), {'kit': 2, 'pm_account': 3})
        self.assertEqual(self.crossing()['phase'], U.STARTED)

    def test_a_failed_start_counts_and_fails_the_run_with_the_state_kept(self):
        self.ready()
        self.code = 3
        with self.assertRaises(U.StepFailed):
            self.run_job()
        self.assertEqual(self.crossing()['phase'], U.STARTING)
        self.assertEqual(self.doc()['starts'], {'kit': [NOW]})

    def test_a_timeout_fails_the_run_too(self):
        self.ready()
        self.code = subprocess.TimeoutExpired('x', 1)
        with self.assertRaisesRegex(U.StepFailed, 'did not end'):
            self.run_job()

    def test_a_stop_refused_fails_the_run_and_stays_stopping(self):
        self.put(crossing(U.STOPPING))
        self.write('sessions.json', {'kit': MOVED})
        self.live = [OLD, NEW]
        self.code = 2
        with self.assertRaises(U.StepFailed):
            self.run_job()
        self.assertEqual(self.crossing()['phase'], U.STOPPING)

    def test_a_full_stall_stops_first_then_starts_on_the_entrys_note(self):
        self.mode = 'full'
        self.usage['1'] = reading(50)
        self.stall(OLD.name, OLD.pid, '1')
        self.run_job()
        self.assertEqual(self.calls, [['stop', '--name', OLD.name, '--pid', '11']])
        self.assertEqual(self.saved_at_child['crossings']['kit']['phase'], U.STALL_STOP)
        self.assertEqual(self.ctx.events, [('stalled', 'kit:a2')])
        self.live = []
        self.now += 300
        self.run_job()
        self.assertEqual(self.calls[-1], ['handoff', '--session', 'kit', '--account', '2'])

    def test_a_stall_with_a_successor_up_names_it_to_the_stop(self):
        self.mode = 'full'
        self.put(crossing(U.STARTED))
        self.write('sessions.json', {'kit': MOVED})
        self.live = [OLD, NEW]
        self.stall(OLD.name, OLD.pid, '1')
        self.run_job()
        self.assertEqual(self.calls, [['stop', '--name', OLD.name, '--successor', NEW.name,
                                       '--pid', '11']])

    def test_a_stall_read_of_a_dead_record_is_not_a_stall(self):
        self.mode = 'full'
        self.usage['1'] = reading(50)
        self.stall(OLD.name, 999, '1')          # the registry keeps a dead process's record
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.calls, [])

    def test_the_stall_read_needs_no_usage_reading_or_watcher_event(self):
        self.mode = 'full'
        self.usage['1'] = None
        self.stall(OLD.name, OLD.pid, '1')
        self.run_job()
        self.assertEqual(self.calls[0][:3], ['stop', '--name', OLD.name])

    def test_one_acting_step_per_run_a_stop_before_an_ask(self):
        other = U.Live('zed-coord-a2', 33, CREATED)
        self.write('sessions.json', {'kit': MOVED, 'zed': dict(ENTRY, stem='zed-coord', name=other.name,
                                                               account=2)})
        self.usage = {'1': reading(50), '2': reading(99), '3': reading(20)}
        self.put(crossing(U.STOPPING))
        self.live = [OLD, NEW, other]
        self.run_job()
        self.assertEqual(self.calls[0][0], 'stop')
        self.assertEqual(self.sent, [])
        self.assertEqual(len(self.calls), 1)

    def test_every_key_is_still_decided_on_a_run_that_acts(self):
        self.write('sessions.json', {'kit': dict(ENTRY), 'zed': dict(ENTRY, stem='zed', name='zed-a3')})
        self.write(U.STATE_FILE, {'crossings': {'zed': crossing(U.ASKED, old_name='zed-a3', old_pid=9)}})
        self.run_job()
        self.assertEqual(len(self.sent), 1)
        self.assertIn(('cross-cleared', 'zed'), self.ctx.events)      # zed has gone

    def test_a_hub_closing_acts_on_nothing(self):
        ctx = Ctx()
        ctx.stop.set()
        self.assertIsInstance(self.run_job(ctx), jobs.Skip)
        self.assertEqual(self.sent, [])

    def test_a_bad_state_file_fails_the_run_and_acts_on_nothing(self):
        with open(os.path.join(self.state, U.STATE_FILE), 'w') as f:
            f.write('{ not json')
        with self.assertRaises(LaunchError):
            self.run_job()
        self.assertEqual(self.sent, [])

    def test_no_handoff_read_yet_skips(self):
        self.job.handoffs = lambda: None
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual(self.sent, [])

    def test_a_held_no_room_is_pushed_once_until_it_changes(self):
        self.usage = {'1': reading(99), '2': reading(99), '3': reading(99)}
        self.run_job()
        self.assertEqual(self.ctx.events, [('no-room', 'kit')])
        self.run_job()
        self.assertEqual(self.ctx.events, [])
        self.usage['1'] = reading(50)
        self.run_job()
        self.usage['1'] = reading(99)
        self.run_job()
        self.assertEqual(self.ctx.events, [('no-room', 'kit')])

    def test_the_log_has_a_line_per_reading_change_and_per_new_decision(self):
        self.usage = {'1': reading(50)}
        self.run_job()
        self.run_job()
        self.usage = {'1': reading(51)}
        self.run_job()
        lines = self.log_text().splitlines()
        self.assertEqual(len([l for l in lines if 'reading account 1' in l]), 2)
        self.assertEqual(len([l for l in lines if 'under the trigger' in l]), 1)


class Notifying(JobBase):
    def setUp(self):
        super().setUp()
        self.mode = 'notify'

    def test_unset_is_notify(self):
        self.assertEqual(lsettings.Settings({}, home=self.tmp).usage_crossover, 'notify')

    def test_it_dry_runs_and_pushes_once_and_changes_nothing(self):
        before = self.read('sessions.json')
        self.run_job()
        self.assertEqual(self.ctx.events, [('would-cross', 'kit:a2')])
        self.assertEqual(self.calls, [
            ['handoff', '--session', 'kit', '--account', '2', '--what-if'],
            ['stop', '--name', OLD.name, '--pid', '11', '--dry-run']])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.doc()['crossings'], {})
        self.assertEqual(self.read('sessions.json'), before)
        self.assertIn('would run handoff: exit 0: said', self.log_text())
        self.assertIsInstance(self.run_job(), jobs.Skip)
        self.assertEqual((self.ctx.events, len(self.calls)), ([], 2))

    def test_a_would_cross_already_told_leaves_the_next_key_its_turn(self):
        other = U.Live('zed-a1', 33, CREATED)
        self.write('sessions.json', {'kit': dict(ENTRY), 'zed': dict(ENTRY, stem='zed', name=other.name)})
        self.live = [OLD, other]
        self.run_job()
        self.run_job()
        self.assertEqual(self.ctx.events, [('would-cross', 'zed:a2')])

    def test_a_failed_dry_run_is_only_logged(self):
        self.code = 2
        self.run_job()
        self.assertEqual(self.ctx.events, [('would-cross', 'kit:a2')])
        self.code = OSError('no')
        self.usage['2'] = reading(90, week_in=DAY)
        self.usage['3'] = reading(90, week_in=2 * DAY)
        self.job.deliver = None
        self.write(U.STATE_FILE, {})
        self.run_job()
        self.assertIn('exit -1: OSError', self.log_text())

    def test_the_mode_is_read_at_each_run_so_no_hub_restart_is_needed(self):
        self.run_job()
        self.assertEqual(self.sent, [])
        self.mode = 'normal'
        self.run_job()
        self.assertEqual(len(self.sent), 1)
        self.mode = 'notify'
        self.run_job()
        self.assertEqual(self.ctx.events, [('cross-cleared', 'kit')])
        self.assertIsNone(self.crossing())


class Others(JobBase):
    def test_the_pm_session_is_never_moved_and_gets_one_push_a_window(self):
        self.write('sessions.json', {'pm': {'stem': 'pm-session', 'name': 'pm-session', 'state': 'x.md'}})
        self.write('coordinators.json', {'pm_account': 1})
        self.live = [U.Live('pm-session', 40, CREATED)]
        self.run_job()
        self.assertEqual(self.ctx.events, [('pm-limit', 'pm')])
        self.run_job()
        self.assertEqual(self.ctx.events, [])
        self.assertEqual(self.sent, [])

    def test_a_pm_by_session_id_or_name_is_skipped(self):
        self.write('sessions.json', {'kit': dict(ENTRY)})
        self.write('coordinators.json', {'pm_session_id': 'kit-coord'})
        self.run_job()
        self.assertEqual(self.sent, [])
        self.write('coordinators.json', {'pm_name': OLD.name})
        self.run_job()
        self.assertEqual(self.sent, [])

    def test_a_placeholder_pm_session_id_matches_nothing(self):
        self.write('coordinators.json', {'pm_session_id': '<kit-coord'})
        self.run_job()
        self.assertEqual(len(self.sent), 1)

    def test_a_stalled_pm_is_pushed_by_its_stall_read(self):
        self.usage['1'] = reading(20)
        self.write('sessions.json', {})
        self.live = [U.Live('pm-session', 40, CREATED)]
        self.stall('pm-session', 40, '1')
        self.run_job()
        self.assertEqual(self.ctx.events, [('pm-limit', 'pm')])

    def test_a_session_with_no_entry_is_pushed_and_left_alone(self):
        self.write('sessions.json', {})
        self.live = [U.Live('stray-a1', 50, CREATED)]
        self.run_job()
        self.assertEqual(self.ctx.events, [('unmanaged', 'stray-a1')])
        self.run_job()
        self.assertEqual((self.ctx.events, self.sent, self.calls), ([], [], []))

    def test_a_new_week_pushes_again(self):
        self.write('sessions.json', {})
        self.live = [U.Live('stray-a1', 50, CREATED)]
        self.run_job()
        self.now += 2 * DAY + 60
        self.usage['1'] = dict(reading(99), updated_at=iso(self.now - 60))
        self.usage['1']['seven_day'] = {'used_percentage': 99, 'resets_at': iso(self.now + 2 * DAY)}
        self.run_job()
        self.assertEqual(self.ctx.events, [('unmanaged', 'stray-a1')])


class Claims(JobBase):
    def test_a_crossing_claims_its_key_until_it_goes_quiet_too_long(self):
        self.put(crossing(U.STARTED, phase_at=NOW - U.CLAIM_TTL_S + 1))
        self.assertEqual(U.claims(self.load(), NOW), {'kit'})
        self.put(crossing(U.STARTED, phase_at=NOW - U.CLAIM_TTL_S))
        self.assertEqual(U.claims(self.load(), NOW), set())

    def test_no_file_claims_nothing(self):
        self.assertEqual(U.claims(self.load(), NOW), set())

    def test_a_bad_file_raises(self):
        with open(os.path.join(self.state, U.STATE_FILE), 'w') as f:
            f.write('nope')
        with self.assertRaises(LaunchError):
            U.claims(self.load(), NOW)


class Make(JobBase):
    def sources(self):
        class S(object):
            home = self.tmp
            push = None
            handoffs = None
        return S()

    def test_it_needs_a_state_dir(self):
        with mock.patch.object(lsettings, 'load', lambda home=None: lsettings.Settings({}, home=home)):
            self.assertIsNone(U.make(self.sources()))

    def test_it_needs_the_hosts_watcher_readers(self):
        with mock.patch.object(lsettings, 'load', lambda home=None: self.load()), \
                mock.patch.object(U.session_watch, 'host_module', side_effect=ImportError('x')):
            self.assertIsNone(U.make(self.sources()))

    def test_it_runs_before_coord_restart_every_five_minutes(self):
        with mock.patch.object(lsettings, 'load', lambda home=None: self.load()), \
                mock.patch.object(U.session_watch, 'host_module', return_value=self.host):
            job = U.make(self.sources())
        self.assertEqual((job.name, job.every_s, job.first_s), (U.NAME, 300, U.FIRST_S))
        self.assertLess(U.FIRST_S, C.FIRST_S)

    def test_a_written_or_uncertain_try_counts_as_delivered_and_no_other(self):
        class Delivery(object):
            state = None

            def deliver(inner, name, text):
                inner.to = name
                return push.Sent(inner.state, name, None, False)
        s = self.sources()
        s.push = Delivery()
        with mock.patch.object(lsettings, 'load', lambda home=None: self.load()), \
                mock.patch.object(U.session_watch, 'host_module', return_value=self.host):
            job = U.make(s)
        for state, delivered in ((push.WRITTEN, True), (push.UNCERTAIN, True), (push.NOT_WRITTEN, False),
                                 (push.NO_SESSION, False)):
            s.push.state = state
            self.assertEqual(job.run.deliver(OLD.name, 'x'), delivered, state)
        self.assertEqual(s.push.to, OLD.name)
        s.push = None
        self.assertFalse(job.run.deliver(OLD.name, 'x'))

    def test_no_push_worker_delivers_nothing_and_a_hub_with_no_read_skips(self):
        s = self.sources()
        with mock.patch.object(lsettings, 'load', lambda home=None: self.load()), \
                mock.patch.object(U.session_watch, 'host_module', return_value=self.host):
            job = U.make(s)
        self.assertIsInstance(job.run(jobs.Ctx(threading.Event(), lambda *a: None)), jobs.Skip)


if __name__ == '__main__':
    unittest.main()
