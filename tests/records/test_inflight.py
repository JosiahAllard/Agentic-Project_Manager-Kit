"""kitlib.records.inflight: the two readers of "a landing is in flight", the wait and its lines.
The command reader runs real (tiny) Python commands; the wait runs on a fake clock."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib.records import inflight, landing


class Clock(object):
    """time.monotonic and time.sleep for a test: sleep moves the clock, and runs `on_sleep`."""

    def __init__(self, on_sleep=None):
        self.now, self.slept, self.on_sleep = 0.0, [], on_sleep

    def clock(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s
        if self.on_sleep is not None:
            self.on_sleep(len(self.slept))


class Looks(object):
    """A reader whose looks are `answers` in turn (the last one repeats)."""

    kind = 'lock'

    def __init__(self, answers, wait_s=inflight.WAIT_S):
        self.answers, self.wait_s, self.looks = list(answers), wait_s, 0

    def look(self, left=None):
        self.looks += 1
        return self.answers[min(self.looks, len(self.answers)) - 1]


class Cfg(object):
    def __init__(self, landing_section):
        self.landing = landing_section

    def section(self, name):
        assert name == 'landing', name
        return dict(self.landing)


class StubLock(object):
    path = 'X:/repo/.git/kit-landing.lock'

    def __init__(self, answer):
        self.answer = answer

    def read(self):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def py(code):
    return ['{python}', '-c', code]


class ReaderChoice(unittest.TestCase):

    def test_unset_is_off(self):
        self.assertIsNone(inflight.reader('X:/repo', Cfg({}), StubLock(None)))
        self.assertIsNone(inflight.reader('X:/repo', Cfg({'in_flight': None}), StubLock(None)))

    def test_an_empty_object_reads_the_kit_lock_and_waits_the_default(self):
        r = inflight.reader('X:/repo', Cfg({'in_flight': {}}), StubLock(None))
        self.assertEqual((r.kind, r.wait_s), ('lock', 100))
        self.assertEqual(inflight.WAIT_S, 100)

    def test_a_command_and_a_wait(self):
        r = inflight.reader('X:/repo', Cfg({'in_flight': {'command': py('0'), 'wait_s': 7}}),
                            StubLock(None))
        self.assertEqual((r.kind, r.wait_s), ('command', 7))
        self.assertEqual(r.argv[0], sys.executable)


class LockReader(unittest.TestCase):

    def test_free(self):
        held, why = inflight.LockReader(StubLock(None)).look()
        self.assertFalse(held)
        self.assertIn('nobody holds the landing lock', why)

    def test_held_names_the_holder(self):
        rec = {'lane': 'full', 'branches': ['feat/x'], 'token': 't', 'phase': 'gate'}
        held, who = inflight.LockReader(StubLock(rec)).look()
        self.assertTrue(held)
        self.assertIn('feat/x', who)

    def test_a_file_that_is_no_record_is_held(self):
        held, who = inflight.LockReader(StubLock(landing.LockError('not a lock record'))).look()
        self.assertTrue(held)
        self.assertIn('treated as held', who)


class CommandReader(unittest.TestCase):

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: os.rmdir(self.root))

    def look(self, code, timeout=inflight.COMMAND_TIMEOUT):
        return inflight.CommandReader(py(code), self.root, timeout=timeout).look()

    def test_exit_0_is_in_flight_and_the_first_line_names_the_holder(self):
        self.assertEqual(self.look('print(); print("lander: feat/x merging"); print("more")'),
                         (True, 'lander: feat/x merging'))

    def test_exit_0_with_nothing_printed_is_still_in_flight(self):
        held, who = self.look('pass')
        self.assertTrue(held)
        self.assertIn('named no holder', who)

    def test_exit_1_is_free(self):
        held, _ = self.look('import sys; sys.exit(1)')
        self.assertFalse(held)

    def test_any_other_exit_cannot_tell_and_is_in_flight(self):
        held, who = self.look('import sys; sys.stderr.write("boom\\n"); sys.exit(2)')
        self.assertTrue(held)
        self.assertIn('cannot tell', who)
        self.assertIn('exited 2 (boom)', who)

    def test_a_command_that_does_not_answer_is_in_flight(self):
        held, who = self.look('import time; time.sleep(60)', timeout=1)
        self.assertTrue(held)
        self.assertIn('did not answer within 1 seconds', who)

    def test_a_look_gets_only_what_is_left_of_the_wait_but_never_under_the_minimum(self):
        given = []

        def run(argv, cwd, timeout):
            given.append(timeout)
            return inflight.FREE, b'', b''
        r = inflight.CommandReader(py('0'), self.root)
        with mock.patch.object(inflight, 'run', run):
            for left in (None, 100, 12.5, 1, 0):
                r.look(left)
        self.assertEqual(given, [30, 30, 12.5, inflight.LOOK_MIN_S, inflight.LOOK_MIN_S])

    def test_a_command_that_cannot_start_is_in_flight(self):
        r = inflight.CommandReader([os.path.join(self.root, 'no-such-program.exe')], self.root)
        held, who = r.look()
        self.assertTrue(held)
        self.assertIn('could not run', who)

    def test_it_runs_in_the_root_and_root_is_substituted(self):
        held, who = self.look('import os; print(os.getcwd() == r"{root}")')
        self.assertEqual((held, who), (True, 'True'))

    def test_a_long_holder_is_cut(self):
        who = self.look('print("x" * 500)')[1]
        self.assertEqual(len(who), inflight.HOLDER_MAX)
        self.assertTrue(who.endswith('...'))


class Wait(unittest.TestCase):

    def test_it_ends_when_a_look_says_free(self):
        c = Clock()
        r = Looks([(True, 'a'), (True, 'b'), (False, 'free')], wait_s=100)
        held, holder, waited = inflight.wait(r, 'first', [].append, c.sleep, c.clock)
        self.assertEqual((held, holder, waited), (False, 'b', 30))
        self.assertEqual(c.slept, [10, 10, 10])

    def test_the_bound_comes_first(self):
        c = Clock()
        said = []
        held, holder, waited = inflight.wait(Looks([(True, 'h')], wait_s=125), 'h', said.append,
                                             c.sleep, c.clock)
        self.assertEqual((held, holder, waited), (True, 'h', 125))
        self.assertEqual(sum(c.slept), 125)
        self.assertEqual(len(said), 2)                 # at 60 s and 120 s
        self.assertTrue(said[0].startswith('landing   still waiting after 60s of 125s: h'), said)

    def test_a_zero_bound_does_not_sleep(self):
        c = Clock()
        self.assertEqual(inflight.wait(Looks([(True, 'h')], wait_s=0), 'h', [].append, c.sleep,
                                       c.clock), (True, 'h', 0))
        self.assertEqual(c.slept, [])


class Settle(unittest.TestCase):

    def test_nothing_in_flight_says_nothing_and_never_sleeps(self):
        c = Clock()
        self.assertEqual(inflight.settle(Looks([(False, 'free')]), True, None, c.sleep, c.clock),
                         (False, []))
        self.assertEqual(c.slept, [])

    def test_no_wait_says_so_and_never_sleeps(self):
        c = Clock()
        held, lines = inflight.settle(Looks([(True, 'h')]), False, None, c.sleep, c.clock)
        self.assertTrue(held)
        self.assertEqual(lines, ['landing   in flight, not waited for (--no-wait): h'])
        self.assertEqual(c.slept, [])

    def test_a_wait_that_ends(self):
        c = Clock()
        said = []
        held, lines = inflight.settle(Looks([(True, 'h'), (False, 'free')], wait_s=50), True,
                                      said.append, c.sleep, c.clock)
        self.assertFalse(held)
        self.assertEqual(said, ['landing   in flight: h; waiting up to 50s (--no-wait goes on now)'])
        self.assertEqual(lines, ['landing   done after 10s'])

    def test_the_bound_goes_on_and_names_the_holder(self):
        c = Clock()
        held, lines = inflight.settle(Looks([(True, 'h'), (True, 'h2')], wait_s=20), True, None,
                                      c.sleep, c.clock)
        self.assertTrue(held)
        self.assertEqual(lines, ['landing   in flight: h; waiting up to 20s (--no-wait goes on now)',
                                 'landing   still in flight after 20s, going on without it: h2'])


    def test_the_time_a_hanging_command_takes_counts_toward_the_bound(self):
        c = Clock()
        timeouts = []

        def hangs(argv, cwd, timeout):
            c.now += timeout                            # each look runs out its whole timeout
            timeouts.append(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)
        r = inflight.CommandReader(py('0'), 'X:/repo', wait_s=100)
        with mock.patch.object(inflight, 'run', hangs):
            held, lines = inflight.settle(r, True, [].append, c.sleep, c.clock)
        self.assertTrue(held)
        self.assertEqual(timeouts, [30, 30, 20])        # the last look gets only what is left
        self.assertEqual(c.slept, [10, 10])
        self.assertEqual(c.now, 100)
        self.assertIn('landing   still in flight after 100s', lines[-1])

    def test_a_given_start_counts_the_time_before_settle(self):
        c = Clock()
        c.now = 95.0
        held, lines = inflight.settle(Looks([(True, 'h')], wait_s=100), True, None, c.sleep,
                                      c.clock, start=0.0)
        self.assertTrue(held)
        self.assertEqual(c.slept, [5])
        self.assertEqual(lines[-1], 'landing   still in flight after 100s, going on without it: h')

if __name__ == '__main__':
    unittest.main()
