"""kitlib.autotasks.due: when a hub schedule is due. Pure: every time is given."""
import datetime
import unittest

from kitlib.autotasks import due

D = datetime.datetime
WED = D(2026, 10, 21, 12, 0)            # a Wednesday, noon


def sched(cadence='daily', at='09:00', days=None, set_at=None):
    s = {'task': 'maint-lint', 'repo': 'demo', 'cadence': cadence, 'at': at}
    if days is not None:
        s['days'] = days
    if set_at is not None:
        s['set_at'] = set_at
    return s


class ParseAt(unittest.TestCase):
    def test_hh_mm_only(self):
        self.assertEqual(due.parse_at('09:05'), (9, 5))
        self.assertEqual(due.parse_at('23:59'), (23, 59))
        for bad in ('9:05', '24:00', '12:60', 'ab:cd', '', None, 905, '09:05:00'):
            self.assertIsNone(due.parse_at(bad), bad)


class LastSlot(unittest.TestCase):
    def test_daily_today_once_the_time_has_passed_else_yesterday(self):
        self.assertEqual(due.last_slot('daily', (9, 0), None, WED), D(2026, 10, 21, 9, 0))
        self.assertEqual(due.last_slot('daily', (13, 0), None, WED), D(2026, 10, 20, 13, 0))
        self.assertEqual(due.last_slot('daily', (12, 0), None, WED), WED)

    def test_weekly_the_latest_listed_day(self):
        self.assertEqual(due.last_slot('weekly', (9, 0), ['Mon', 'Fri'], WED), D(2026, 10, 19, 9, 0))
        self.assertEqual(due.last_slot('weekly', (9, 0), ['Wed'], WED), D(2026, 10, 21, 9, 0))
        # today is listed but its time is still ahead: a week ago
        self.assertEqual(due.last_slot('weekly', (13, 0), ['Wed'], WED), D(2026, 10, 14, 13, 0))

    def test_weekly_with_no_known_day_has_no_slot(self):
        self.assertIsNone(due.last_slot('weekly', (9, 0), [], WED))
        self.assertIsNone(due.last_slot('weekly', (9, 0), ['Someday'], WED))
        self.assertIsNone(due.last_slot('hourly', (9, 0), None, WED))


class Decide(unittest.TestCase):
    def test_off_is_disabled(self):
        self.assertEqual(due.decide(sched('off'), None, WED), (due.DISABLED, None))

    def test_bad_cadence_at_or_days(self):
        self.assertEqual(due.decide(sched('hourly'), None, WED)[0], due.BAD)
        self.assertEqual(due.decide(sched(at='25:00'), None, WED)[0], due.BAD)
        self.assertEqual(due.decide(sched('weekly', days=[]), None, WED)[0], due.BAD)

    def test_no_baseline_is_first(self):
        self.assertEqual(due.decide(sched(), None, WED), (due.FIRST, D(2026, 10, 21, 9, 0)))

    def test_daily_due_once_its_slot_passes_the_last_start(self):
        last = D(2026, 10, 20, 9, 1)
        self.assertEqual(due.decide(sched(), last, WED), (due.DUE, D(2026, 10, 21, 9, 0)))
        self.assertEqual(due.decide(sched(), D(2026, 10, 21, 9, 2), WED)[0], due.NOT_DUE)
        early = D(2026, 10, 21, 8, 59)
        self.assertEqual(due.decide(sched(), last, early)[0], due.NOT_DUE)

    def test_weekly_days(self):
        last = D(2026, 10, 16, 9, 1)                 # last Friday's run
        s = sched('weekly', days=['Mon', 'Fri'])
        self.assertEqual(due.decide(s, last, WED), (due.DUE, D(2026, 10, 19, 9, 0)))
        self.assertEqual(due.decide(s, D(2026, 10, 19, 9, 1), WED)[0], due.NOT_DUE)
        self.assertEqual(due.decide(sched('weekly', days=['Thu']), last, WED)[0], due.NOT_DUE)

    def test_a_missed_slot_runs_once_never_a_burst(self):
        last = D(2026, 10, 14, 9, 1)                 # a week off: six daily slots missed
        verdict, slot = due.decide(sched(), last, WED)
        self.assertEqual((verdict, slot), (due.DUE, D(2026, 10, 21, 9, 0)))
        started = WED                                # the tick records its start
        for minutes in (15, 30, 45, 60 * 20):
            later = WED + datetime.timedelta(minutes=minutes)
            self.assertEqual(due.decide(sched(), started, later)[0], due.NOT_DUE, later)
        self.assertEqual(due.decide(sched(), started, D(2026, 10, 22, 9, 0))[0], due.DUE)

    def test_set_at_is_a_baseline_so_a_new_schedule_waits_for_its_next_slot(self):
        set_at = D(2026, 10, 21, 10, 0).astimezone().isoformat(timespec='seconds')
        self.assertEqual(due.decide(sched(set_at=set_at), None, WED)[0], due.NOT_DUE)
        self.assertEqual(due.decide(sched(set_at=set_at), None, D(2026, 10, 22, 9, 0))[0], due.DUE)
        before = D(2026, 10, 21, 8, 0).astimezone().isoformat(timespec='seconds')
        self.assertEqual(due.decide(sched(set_at=before), None, WED)[0], due.DUE)

    def test_the_later_of_start_and_set_at_counts(self):
        set_at = D(2026, 10, 21, 10, 0).astimezone().isoformat(timespec='seconds')
        self.assertEqual(due.decide(sched(set_at=set_at), D(2026, 10, 1), WED)[0], due.NOT_DUE)


if __name__ == '__main__':
    unittest.main()
