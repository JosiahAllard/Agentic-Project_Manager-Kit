"""tools/redfirst.py and the machine lock (kitlib.redfirst.run lock_kind, one_lock, book_slot,
locked_run): which runs book it, what a booked run's command is given, a run nested in a caller's
run, the lock's refusals, and Ctrl-C around the booking.

Every booking is made on the test's own scratch store (tests/lock/helpers.py StoreCase) with an
environment the test passes, never this process's own MACHINE_LOCK_* variables: LockCase.setUp
fails any kitlib.lock.run.book call that names another store. The repositories and worktrees are
the throwaway ones of tests/redfirst/test_run.py's Case.
"""
import io
import json
import os
import re
import signal
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kitlib.config import heavy_programs
from kitlib.redfirst import run as R
from kitlib.redfirst import verdict as V
from tests.lock.helpers import KINDS, StoreCase
from tests.lock.test_run import one_usable_slot

from .test_run import Case, TEST_ADD, TEST_MUL, tool
from tests.platforms import windows_only

if os.name == 'nt':
    from kitlib.lock import run as lockrun
    from kitlib.lock.store import EXIT_BUSY, EXIT_TIMEOUT
    from kitlib.proc import process

RID = 'r-20261006T120000Z-abcdef'       # the run a nested test's caller holds
GRADLE = {'glob': 'app/*Test.kt', 'kind': 'gradle', 'dir': 'app', 'project': ':app'}
DOTNET = {'glob': 'cs/*Tests.cs', 'kind': 'dotnet', 'csproj': 'cs/T.csproj'}
PY_LOCK = {'glob': 'tests/test_*.py', 'kind': 'python', 'pythonpath': 'tests', 'lock': 'engine-test'}
PY_PLAIN = {'glob': 'src/test_*.py', 'kind': 'python', 'pythonpath': 'src'}
PY_ODD = {'glob': 'odd/test_*.py', 'kind': 'python', 'pythonpath': 'odd', 'lock': 'no-such-kind'}

# A test that fails with the run its process was started in: red, and its log names the run.
SHOW_RUN = ('import os\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
            '    def test_it(self):\n'
            "        self.fail('LOCKRUN=%s' % os.environ.get('MACHINE_LOCK_RUN'))\n")
# A test that asks the lock to stop its own run (what the lock's stop writes), then outlasts it.
STOP_SELF = ('import os\nimport time\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
             '    def test_it(self):\n'
             "        rid = os.environ['MACHINE_LOCK_RUN'].split(':')[0]\n"
             "        open(os.path.join(os.environ['RF_ACTIVE'], rid + '.stop'), 'w').close()\n"
             '        time.sleep(60)\n')


class Kinds(unittest.TestCase):
    """lock_kind and one_lock: pure, from the runner and the heavy list."""

    def sel(self, text, *runners):
        return V.parse_selector(text, list(runners))

    def test_the_heavy_list_gives_gradle_and_dotnet_the_build_kind_and_python_none(self):
        heavy = heavy_programs()
        for text, runner, want in (('app/FooTest.kt', GRADLE, 'build'),
                                   ('cs/FooTests.cs', DOTNET, 'build'),
                                   ('src/test_add.py', PY_PLAIN, None)):
            with self.subTest(text=text):
                kind, where = R.lock_kind(self.sel(text, runner), heavy)
                self.assertEqual(kind, want)
                if want:
                    self.assertIn('the heavy list takes', where)
                else:
                    self.assertIsNone(where)

    def test_a_runner_s_lock_wins_over_the_heavy_list(self):
        heavy = heavy_programs()
        kind, where = R.lock_kind(self.sel('tests/test_mul.py', PY_LOCK), heavy)
        self.assertEqual(kind, 'engine-test')
        self.assertIn("the `lock` of the runner for glob 'tests/test_*.py'", where)
        named = dict(GRADLE, lock='cpu-eval')
        self.assertEqual(R.lock_kind(self.sel('app/FooTest.kt', named), heavy)[0], 'cpu-eval')

    def test_one_run_takes_one_kind(self):
        heavy = heavy_programs()
        plain = self.sel('src/test_add.py', PY_PLAIN)
        gradle = self.sel('app/FooTest.kt', GRADLE)
        locked = self.sel('tests/test_mul.py', PY_LOCK)
        self.assertEqual(R.one_lock([plain], heavy), (None, None))
        self.assertEqual(R.one_lock([plain, gradle, gradle], heavy)[0], 'build')
        with self.assertRaises(V.Usage) as cm:
            R.one_lock([locked, plain, gradle], heavy)
        text = str(cm.exception)
        self.assertIn('one run books the machine lock once', text)
        for line in ('  tests/test_mul.py: engine-test', '  src/test_add.py: no lock',
                     '  app/FooTest.kt: build'):
            self.assertIn(line, text)


class BookMode(unittest.TestCase):
    """Interrupts' BOOK mode: a signal raises only while `wait_code` is running."""

    def test_it_raises_inside_the_wait_only_and_once(self):
        ints = None

        def signalled():
            ints.handler(signal.SIGINT, sys._getframe())

        def wait():
            signalled()

        ints = R.Interrupts(wait_code=wait.__code__)
        ints.mode = R.BOOK
        signalled()                                 # outside the wait: noted, book goes on
        self.assertEqual((ints.seen, ints.mode), (1, R.BOOK))
        with self.assertRaises(KeyboardInterrupt):
            wait()                                  # inside it: raised, and held from then on
        self.assertEqual((ints.seen, ints.mode), (2, R.HOLD))
        wait()
        self.assertEqual((ints.seen, ints.mode), (3, R.HOLD))

    @windows_only('the lock is Windows only')
    def test_the_wait_is_the_lock_s_queue_wait(self):
        self.assertIs(R.Interrupts().wait_code, lockrun.acquire.__code__)


class LockCase(Case, StoreCase):
    """A fixture repository (Case) whose runs book on a scratch store (StoreCase) that has one
    usable cpu slot: a second booking waits or is refused."""
    KIT_JSON = {'repo': {'name': 'fixture'},
                'redfirst': {'runners': [PY_LOCK, PY_PLAIN, PY_ODD, GRADLE]}}

    def setUp(self):
        StoreCase.setUp(self)
        with open(self.kinds_path, 'w', encoding='utf-8') as f:
            json.dump({'kinds': dict(KINDS['kinds'],
                                     build={'cpu': 'medium', 'gpu': 0, 'warn_min': 60})}, f)
        one_usable_slot(self)
        Case.setUp(self)
        self.env = {k: v for k, v in os.environ.items()
                    if k not in ('CLAUDE_CODE_SESSION_ID', 'CLAUDE_PID')
                    and not k.startswith('MACHINE_LOCK_')}
        self.slug = R.PREFIX + self.base[:10]
        self.booked = []
        self.real_book = lockrun.book
        guard = mock.patch.object(lockrun, 'book', self.checked_book)
        guard.start()
        self.addCleanup(guard.stop)

    def checked_book(self, kind, repo, slug, **kw):
        store = kw.get('store')
        if store is None or os.path.normcase(store.root) != os.path.normcase(self.store.root):
            raise AssertionError('a test booked the machine lock outside its scratch store')
        self.booked.append((kind, repo, slug))
        return self.real_book(kind, repo, slug, **kw)

    def run_locked(self, *args, run=None, cleanup=None, **book):
        """(exit code, output) of tools/redfirst.py; `book` changes what it passes to book."""
        book_kw = dict({'cores': 1, 'store': self.store, 'env': self.env}, **book)
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tool.main(['-C', self.wt, '--log-dir', self.logs] + list(args), run=run,
                           cleanup=cleanup or self.here, book_kw=book_kw)
        return rc, out.getvalue()

    def records(self, folder):
        d = self.store.path(folder)
        return sorted(n for n in (os.listdir(d) if os.path.isdir(d) else [])
                      if n.startswith('r-') and n.endswith('.json'))

    def ended(self):
        """The ended run records, but the blocker's."""
        recs = [self.store.read('ended/' + n) for n in self.records('ended')]
        return [r for r in recs if r['slug'] != 'blocker']

    def entries(self):
        return [(e['slug'], e['state'])
                for e in (self.store.read('queue.json') or {}).get('entries', [])]

    def blocker(self):
        """A run that holds the store's one usable slot until the test ends."""
        slot = self.real_book('engine-test', 'kit', 'blocker', cores=1, store=self.store,
                              env=self.env, no_wait=True)
        self.addCleanup(slot.release)
        return slot

    def log(self, name):
        with open(os.path.join(self.logs, name), encoding='utf-8', errors='replace') as f:
            return f.read()

    def nothing_made(self, out):
        self.assertNotIn('tree      ', out)
        self.assertEqual(self.throwaways(), [])
        self.assertEqual(os.listdir(self.logs), ['summary.txt'])     # the folder, made first, holds only that


class Booking(LockCase):
    def test_a_runner_with_a_lock_kind_books_and_its_command_runs_in_the_run(self):
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        at_cleanup = []

        def cleanup(*a):
            at_cleanup.append((self.records('active'), self.entries()))
            return self.here(*a)
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', cleanup=cleanup)
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertEqual(self.booked, [('engine-test', 'fixture', self.slug)])
        rec, = self.ended()
        self.assertRegex(out, r'(?m)^lock      engine-test holds cpu slot \d+ for fixture/%s '
                              r'\(engine-test, run %s\); the command runs on '
                         % (self.slug, rec['id']))
        self.assertLess(out.index('lock      engine-test holds'), out.index('tree      '))
        # The command ran in the booked run: its environment names it, with this process (the
        # run's wrapper) as the pid a lock call inside it would nest under.
        self.assertEqual(rec['pid'], os.getpid())
        self.assertIn('LOCKRUN=%s:%d:%d' % (rec['id'], os.getpid(), process.own_ctime()),
                      self.log('1-python-test_lockrun.log'))
        self.assertEqual((rec['repo'], rec['slug'], rec['kind'], rec['how'], rec['code']),
                         ('fixture', self.slug, 'engine-test', 'done', V.EXIT_RED))
        self.assertEqual(rec['cmd'], ['redfirst.py', '--tests', 'tests/test_lockrun.py'])
        self.assertEqual(at_cleanup, [([], [])])    # released before the cleanup began
        self.assertEqual((self.records('active'), self.entries(), self.throwaways()), ([], [], []))

    def test_two_tests_of_one_kind_share_one_booking(self):
        self.commit({'tests/test_lockrun.py': SHOW_RUN, 'tests/test_mul.py': TEST_MUL})
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', 'tests/test_mul.py')
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertEqual(self.booked, [('engine-test', 'fixture', self.slug)])
        rec, = self.ended()
        self.assertIn('LOCKRUN=%s:' % rec['id'], self.log('1-python-test_lockrun.log'))
        self.assertTrue(os.path.isfile(os.path.join(self.logs, '2-python-test_mul.log')))
        self.assertEqual(rec['cmd'],
                         ['redfirst.py', '--tests', 'tests/test_lockrun.py', 'tests/test_mul.py'])

    def test_the_run_s_record_ends_with_the_verdict_code_not_the_command_s(self):
        # The command exits 0 (the test passes at the base), and a pass is redfirst's exit 1.
        self.commit({'tests/test_add.py': TEST_ADD})
        rc, out = self.run_locked('--tests', 'tests/test_add.py')
        self.assertEqual(rc, V.EXIT_GREEN, out)
        self.assertIn('REDFIRST NOT RED', out)
        self.assertEqual([(r['how'], r['code']) for r in self.ended()], [('done', V.EXIT_GREEN)])

    def test_a_gradle_command_books_the_heavy_list_s_build_kind(self):
        self.commit({'app/FooTest.kt': 'class FooTest\n'})
        seen = {}

        def run(argv, cwd, env, log):
            seen.update(program=os.path.basename(argv[0]), entries=self.entries(),
                        active=len(self.records('active')))
            with open(log, 'w', encoding='utf-8') as f:
                f.write('no gradle here\n')
            return 1
        rc, out = self.run_locked('--tests', 'app/FooTest.kt', run=run)
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertEqual(self.booked, [('build', 'fixture', self.slug)])
        self.assertIn('lock      build holds cpu slot', out)
        self.assertEqual(seen, {'program': 'gradlew.bat', 'entries': [(self.slug, 'held')],
                                'active': 1})      # held while the command ran
        self.assertIn('gradle exited 1', out)
        self.assertEqual([(r['kind'], r['how'], r['code']) for r in self.ended()],
                         [('build', 'done', V.EXIT_UNDECIDED)])
        self.assertEqual((self.records('active'), self.entries(), self.throwaways()), ([], [], []))

    def test_the_default_store_says_the_lock_s_lines_as_lock_lines(self):
        # No store passed: book_slot makes one (here a scratch one, through the name it uses).
        self.commit({'tests/test_add.py': TEST_ADD})
        made = []

        def scratch(say=None):
            store = self.make_store()
            store.say = say
            made.append(store)
            return store
        with mock.patch.object(R, 'Store', scratch):
            rc, out = self.run_locked('--tests', 'tests/test_add.py', store=None,
                                      env=dict(self.env, **{lockrun.NEST_ENV: 'junk'}))
        self.assertEqual(rc, V.EXIT_GREEN, out)
        self.assertEqual(len(made), 1)
        self.assertIn('lock      MACHINE_LOCK_RUN ignored (malformed value); this run takes its '
                      'own lock', out)
        self.assertIn('lock      engine-test holds cpu slot', out)

    def test_a_held_slot_whose_lock_line_cannot_be_said_is_given_back(self):
        # book_slot holds the slot before execute has it: were the line's failure to leave it
        # there, the run would stay booked until a prune found its holder dead.
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        failed = []

        def say(line):
            if line.startswith('lock      engine-test holds'):
                failed.append(line)
                raise OSError('the output is gone')
            print(line, flush=True)
        with mock.patch.object(tool, 'say', say):
            rc, out = self.run_locked('--tests', 'tests/test_lockrun.py')
        self.assertEqual(len(failed), 1, out)
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertIn('failed    unexpected OSError: the output is gone', out)
        self.assertEqual([(r['how'], r['code'], r['slug'], len(r['cores'])) for r in self.ended()],
                         [('error', 1, self.slug, 1)])              # it held its slot, and let it go
        self.assertEqual((self.records('active'), self.entries()), ([], []))
        self.nothing_made(out)


class NoBooking(LockCase):
    def test_a_python_runner_with_no_lock_books_nothing(self):
        self.commit({'src/test_add.py': TEST_ADD})
        rc, out = self.run_locked('--tests', 'src/test_add.py')
        self.assertEqual(rc, V.EXIT_GREEN, out)
        self.assertEqual(self.booked, [])
        self.assertNotIn('lock      ', out)
        self.assertEqual((self.records('active'), self.records('ended'), self.entries()),
                         ([], [], []))

    def test_two_kinds_in_one_run_are_refused_before_anything_is_made(self):
        self.commit({'tests/test_mul.py': TEST_MUL, 'app/FooTest.kt': 'class FooTest\n'})
        rc, out = self.run_locked('--tests', 'tests/test_mul.py', 'app/FooTest.kt')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn('refused: one run books the machine lock once, and these tests need '
                      'different kinds (engine-test, build); nothing was run', out)
        self.assertIn('  tests/test_mul.py: engine-test', out)
        self.assertIn('  app/FooTest.kt: build', out)
        self.assertEqual(self.booked, [])
        self.assertFalse(os.path.exists(self.logs))
        self.assertEqual(self.throwaways(), [])


class Nested(LockCase):
    """redfirst started inside a run a caller already holds (MACHINE_LOCK_RUN names it)."""

    def caller_holds(self, slots):
        """The MACHINE_LOCK_RUN value of a held run whose wrapper is this process's parent."""
        pp = os.getppid()
        ct = process.proc_ctime(pp)
        if ct is None or (pp, ct) not in process.own_ancestors():
            self.skipTest("this process's parent cannot be read as an ancestor")
        self.live_record(RID, pid=pp, ctime=ct, has={'cpu': slots}, cores=list(range(slots)))
        return '%s:%d:%d' % (RID, pp, ct)

    def test_inside_a_run_that_holds_enough_it_takes_nothing_more(self):
        token = self.caller_holds(2)
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', cores=None,
                                  env=dict(self.env, **{lockrun.NEST_ENV: token}))
        self.assertEqual(rc, V.EXIT_RED, out)
        self.assertIn('lock      nested: run %s already holds what it needs; the command runs '
                      'in its job' % RID, out)
        self.assertIn('LOCKRUN=%s' % token, self.log('1-python-test_lockrun.log'))
        self.assertEqual((self.entries(), self.ended()), ([], []))      # no second booking
        self.assertEqual(self.records('active'), [RID + '.json'])
        self.assertEqual(self.store.read('active/%s.json' % RID)['state'], 'held')
        self.assertEqual(self.throwaways(), [])

    def test_inside_a_run_that_holds_fewer_slots_than_the_kind_needs_it_is_refused(self):
        token = self.caller_holds(1)
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', cores=None,
                                  env=dict(self.env, **{lockrun.NEST_ENV: token}))
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn('refused: the machine lock: nested run refused: a engine-test run needs ',
                      out)
        self.assertIn('; start it outside that run (kind engine-test: the `lock` of the runner '
                      "for glob 'tests/test_*.py' in `.kit.json` \"redfirst\".runners); no tree "
                      'was made and nothing was run', out)
        self.assertNotIn('Exit 2.', out)
        self.nothing_made(out)
        self.assertEqual((self.entries(), self.ended()), ([], []))
        self.assertEqual(self.store.read('active/%s.json' % RID)['state'], 'held')


class Refused(LockCase):
    def test_a_kind_the_kinds_table_does_not_have_is_a_usage_refusal(self):
        self.commit({'odd/test_x.py': SHOW_RUN})
        rc, out = self.run_locked('--tests', 'odd/test_x.py')
        self.assertEqual(rc, V.EXIT_USAGE, out)
        self.assertIn("refused: the machine lock: unknown kind 'no-such-kind' (known: build, ", out)
        self.assertIn('(kind no-such-kind: the `lock` of the runner for glob', out)
        self.assertEqual(self.booked, [('no-such-kind', 'fixture', self.slug)])
        self.nothing_made(out)

    def test_a_busy_lock_passes_its_own_exit_code_through(self):
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        self.blocker()
        for book, code in (({'no_wait': True}, EXIT_BUSY), ({'wait_s': 0.3}, EXIT_TIMEOUT)):
            with self.subTest(code=code):
                rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', **book)
                self.assertEqual(rc, code, out)
                self.assertIn('NOT RUN: the machine lock refused the run: NOT ACQUIRED', out)
                self.assertIn('REDFIRST NOT RUN - the machine lock refused a engine-test run '
                              '(exit %d); no tree was made and nothing was run' % code, out)
                self.nothing_made(out)
        self.assertEqual((EXIT_BUSY, EXIT_TIMEOUT), (6, 8))     # neither reads as RED or GREEN
        self.assertEqual(sorted((r['how'], r['code']) for r in self.ended()),
                         [('not acquired', 6), ('not acquired', 8)])
        self.assertEqual([slug for slug, _ in self.entries()], ['blocker'])


class Stopped(LockCase):
    def test_the_lock_s_stop_ends_the_command_and_gives_no_verdict(self):
        self.commit({'tests/test_stop.py': STOP_SELF})
        said = []
        self.store.say = said.append
        rc, out = self.run_locked('--tests', 'tests/test_stop.py',
                                  env=dict(self.env, RF_ACTIVE=self.store.path('active')))
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        rec, = self.ended()
        self.assertIn('failed    the machine lock stopped run %s; its command was ended and '
                      'gives no verdict' % rec['id'], out)
        self.assertIn('run %s was cancelled by stop; its process tree is ended' % rec['id'], said)
        self.assertEqual((rec['how'], rec['code']), ('cancelled', 1))
        self.assertEqual((self.records('active'), self.entries(), self.throwaways()), ([], [], []))

    def test_the_lock_s_stop_while_it_waits_is_undecided_never_green(self):
        # The lock ends a stopped wait with its code 1, which as this tool's exit would read GREEN.
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        held = self.blocker()
        asked = []

        def say(text):
            m = re.match(r'run (\S+) waits in the queue', text)
            if m and not asked:
                asked.append(m.group(1))
                open(self.store.path('active', m.group(1) + '.stop'), 'w').close()
        self.store.say = say
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', wait_s=20)
        self.assertEqual(len(asked), 1, out)
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertIn('failed    the machine lock: run %s was cancelled by stop while it waited; '
                      'the command was not run' % asked[0], out)
        self.assertIn('REDFIRST UNDECIDED - no tree was made', out)
        self.assertEqual([r['id'] for r in self.ended()], asked)
        self.assertEqual(self.entries(), [('blocker', 'held')])
        self.assertEqual(self.records('active'), [held.id + '.json'])
        self.nothing_made(out)

    def test_ctrl_c_while_it_waits_in_the_queue_ends_the_wait(self):
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        held = self.blocker()
        before = signal.getsignal(signal.SIGINT)
        waits = []

        def say(text):
            if 'waits in the queue' in text:
                waits.append(text)
                if len(waits) == 1:
                    signal.raise_signal(signal.SIGINT)          # Ctrl-C, once it has joined
        self.store.say = say
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py', wait_s=20)
        self.assertEqual(len(waits), 1, out)
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertIn('stopped   interrupted while waiting for the machine lock; no tree was '
                      'made', out)
        self.assertEqual([(r['how'], r['code'], r['slug']) for r in self.ended()],
                         [('interrupted', 130, self.slug)])
        self.assertEqual(self.entries(), [('blocker', 'held')])
        self.assertEqual(self.records('active'), [held.id + '.json'])
        self.nothing_made(out)
        self.assertIs(signal.getsignal(signal.SIGINT), before)

    def test_ctrl_c_outside_the_wait_is_held_and_the_slot_is_given_back(self):
        # The signal arrives in book before its queue wait: a raise there could lose the Slot,
        # so it is only noted; book returns the held run and book_slot gives it back.
        self.commit({'tests/test_lockrun.py': SHOW_RUN})
        told = []

        def say(text):
            if 'ignored' in text:
                told.append(text)
                signal.raise_signal(signal.SIGINT)
        self.store.say = say
        rc, out = self.run_locked('--tests', 'tests/test_lockrun.py',
                                  env=dict(self.env, **{lockrun.NEST_ENV: 'junk'}))
        self.assertEqual(len(told), 1, out)
        self.assertEqual(rc, V.EXIT_UNDECIDED, out)
        self.assertIn('stopped   interrupted before the tree was made', out)
        self.assertNotIn('while waiting for the machine lock', out)
        rec, = self.ended()
        self.assertEqual((rec['how'], rec['code']), ('interrupted', 130))
        self.assertEqual(len(rec['cores']), 1)                  # it held its slot, and let it go
        self.assertEqual((self.records('active'), self.entries()), ([], []))
        self.nothing_made(out)


if __name__ == '__main__':
    unittest.main()
