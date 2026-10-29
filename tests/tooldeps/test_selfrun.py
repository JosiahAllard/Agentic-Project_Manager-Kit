"""kitlib.tooldeps.selfrun: the kit's own test modules in parallel worker processes (docs/test-tools.md
"kit_tests"). The ordering, Ctrl+C and log cases use a fake unit runner; the counting, isolation
and timeout cases run real child processes on throwaway test modules.
"""
import _thread
import io
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from kitlib.config import user
from kitlib.proc import shield
from kitlib.tooldeps import selfrun
from tests.fs.test_rmtree import read_only_repo
from tests.gitfixture import rmtree
from tests.home import TempHomeCase
from tests.platforms import windows_only


def fake_unit(log, lock, status=None, ran=3, pause=0.4):
    """A run_unit that records (start|end, module, TEMP) and sleeps; `status` {module: status}."""
    status = status or {}

    def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
        with lock:
            log.append(('start', unit[0], env['TEMP']))
        time.sleep(pause)
        with lock:
            log.append(('end', unit[0], env['TEMP']))
        st = status.get(unit[0], 'OK')
        return unit, st, ran, pause, 'output of %s\n' % unit[0]
    return run_unit


def most_at_once(log):
    now = best = 0
    for what, _m, _t in log:
        now += 1 if what == 'start' else -1
        best = max(best, now)
    return best


def write(root, files):
    for rel, text in files.items():
        full = os.path.join(root, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as fh:
            fh.write(text)


class Jobs(unittest.TestCase):
    def test_the_lock_runs_mask_sets_the_jobs(self):
        self.assertEqual(selfrun.default_jobs({'MACHINE_LOCK_CPUMASK': '0xf0'})[0], 4)
        n, why = selfrun.default_jobs({'MACHINE_LOCK_CPUMASK': '0x3f000'})
        self.assertEqual(n, 6)
        self.assertIn('lock run holds', why)

    def test_outside_a_lock_run_the_fallback_says_so(self):
        n, why = selfrun.default_jobs({})
        self.assertEqual(n, selfrun.FALLBACK_JOBS)
        self.assertIn('not under a machine lock run', why)
        n, why = selfrun.default_jobs({'MACHINE_LOCK_CPUMASK': 'zz'})
        self.assertEqual(n, selfrun.FALLBACK_JOBS)
        self.assertIn('not a processor mask', why)

    @windows_only('the lock is Windows only')
    def test_the_variable_is_the_one_the_lock_sets(self):
        from kitlib.lock import giveback
        from kitlib.lock import run as lock_run
        self.assertEqual(selfrun.CPUMASK_ENV, lock_run.CPUMASK_ENV)
        self.assertEqual(selfrun.GIVE_ENV, giveback.GIVE_ENV)


@windows_only('the Git for Windows launcher')
class GitDirect(unittest.TestCase):
    def setUp(self):
        self.git = tempfile.mkdtemp(prefix='kit-selfrun-')
        self.addCleanup(rmtree, self.git)
        write(self.git, {'cmd/git.exe': '', 'mingw64/bin/git.exe': ''})
        self.cmd = os.path.join(self.git, 'cmd')
        self.direct = os.path.join(self.git, 'mingw64', 'bin')

    def test_the_launcher_on_path_puts_its_own_git_first(self):
        env, note = selfrun.git_direct_env({'Path': self.cmd + os.pathsep + r'C:\x', 'A': 'b'})
        self.assertEqual(env, {'Path': os.pathsep.join([self.direct, self.cmd, r'C:\x']), 'A': 'b'})
        self.assertIn('first on PATH', note)

    def test_a_direct_git_or_no_git_leaves_the_environment_alone(self):
        for env in ({'PATH': self.direct}, {'PATH': os.path.join(self.git, 'none')}, {'X': '1'}):
            self.assertEqual(selfrun.git_direct_env(env), (env, None))

    def test_a_launcher_without_its_git_is_left_alone(self):
        os.remove(os.path.join(self.direct, 'git.exe'))
        env = {'PATH': self.cmd}
        self.assertEqual(selfrun.git_direct_env(env), (env, None))


SKIP_OK = 'Ran 3 tests in 0.1s\n\nOK (skipped=2)\n'
NO_SKIP = 'Ran 3 tests in 0.1s\n\nOK\n'


class Skips(TempHomeCase):
    # TempHomeCase (tests/home.py): selfrun.run makes its run folder through
    # kitlib.config.user.make_temp, which reads temp_dir from an empty machine config.
    MODS = ['tests.a.test_1', 'tests.a.test_2']

    def run_texts(self, texts, skip_fails=()):
        def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
            return unit, 'OK', 3, 0.0, texts[unit[0]]
        out = io.StringIO()
        rc, summary = selfrun.run(self.MODS, tempfile.gettempdir(), 2, out=out, run_unit=run_unit,
                                  label='full', counts={}, skip_fails=skip_fails)
        return rc, summary, out.getvalue()

    def test_skipped_of_reads_unittests_summary(self):
        self.assertEqual(selfrun.skipped_of(SKIP_OK), 2)
        self.assertEqual(selfrun.skipped_of('Ran 4 tests in 0s\n\nFAILED (failures=1, skipped=2)\n'), 2)
        self.assertEqual(selfrun.skipped_of(NO_SKIP), 0)
        self.assertEqual(selfrun.skipped_of('no summary at all'), 0)

    def test_skipped_of_matches_a_real_unittest_run(self):
        import subprocess
        import sys
        code = ('import unittest\nclass T(unittest.TestCase):\n'
                '    def test_a(self): self.skipTest("runtime")\n'
                '    @unittest.skip("static")\n    def test_b(self): pass\n'
                '    def test_c(self): pass\n'
                'unittest.main(argv=["x"])\n')
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True)
        self.assertEqual(selfrun.skipped_of(r.stderr), 2, r.stderr)

    def test_line_shows_skipped_count(self):
        rc, summary, out = self.run_texts({m: SKIP_OK for m in self.MODS})
        self.assertEqual(rc, 0, out)
        self.assertRegex(out, r'(?m)^  \[\d/2\] OK +\d+s +3 tests, 2 skipped  tests\.a\.test_[12]$')
        self.assertRegex(out, r'tests/a +OK +6 tests +2 module\(s\) +\d+s  4 skipped')
        self.assertTrue(summary.endswith('6 tests in 2 module(s), 1 package(s), 4 skipped'), summary)

    def test_line_without_skips_is_unchanged(self):
        rc, summary, out = self.run_texts({m: NO_SKIP for m in self.MODS})
        self.assertRegex(out, r'(?m)^  \[\d/2\] OK +\d+s +3 tests  tests\.a\.test_[12]$')
        self.assertNotIn('skipped', out)
        self.assertTrue(summary.endswith('6 tests in 2 module(s), 1 package(s)'), summary)

    def test_skip_fails_module_with_a_skip_is_failed(self):
        rc, summary, out = self.run_texts({m: SKIP_OK for m in self.MODS},
                                          [('tests.a.test_1', 'the proof')])
        self.assertEqual(rc, 1, out)
        self.assertIn('failed: tests.a.test_1 (FAILED)', summary)
        self.assertNotIn('tests.a.test_2 (', summary)
        self.assertIn('proof module', out)
        self.assertIn('the proof', out)

    def test_skip_fails_module_without_a_skip_stays_ok(self):
        rc, _summary, out = self.run_texts({m: NO_SKIP for m in self.MODS},
                                           [('tests.a.test_1', 'the proof')])
        self.assertEqual(rc, 0, out)

    def test_a_skip_in_an_unlisted_module_is_only_counted(self):
        rc, _summary, out = self.run_texts({m: SKIP_OK for m in self.MODS},
                                           [('tests.z.test_other', 'x')])
        self.assertEqual(rc, 0, out)


class Order(TempHomeCase):
    # TempHomeCase: selfrun.run's run folder comes from user.make_temp (an empty machine config).
    MODS = ['tests.a.test_1', 'tests.a.test_2', 'tests.b.test_1', 'tests.b.test_2',
            'tests.c.test_1', 'tests.c.test_2']

    def run_fake(self, jobs, serial=(), status=None, run_unit=None, log=None):
        log = [] if log is None else log
        out = io.StringIO()
        rc, summary = selfrun.run(self.MODS, tempfile.gettempdir(), jobs, serial=serial, out=out,
                                  run_unit=run_unit or fake_unit(log, threading.Lock(), status),
                                  environ={'X': '1'}, label='full', counts={})
        return rc, summary, out.getvalue(), log

    def test_no_pid_hold_unless_the_caller_passes_one(self):
        made = []
        with mock.patch.object(shield, 'hold', lambda: made.append(1)):
            rc, _summary, out, _log = self.run_fake(2)
        self.assertEqual((rc, made), (0, []), out)
        self.assertNotIn('pid hold', out)

    def test_the_holds_end_line_comes_after_the_units_and_before_the_summary(self):
        log = []

        class Hold(object):
            def end(self):
                log.append('end')
                return 'pid hold: held to the end of the run'

        def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
            log.append('unit')
            return unit, 'OK', 1, 0.0, ''
        out = io.StringIO()
        rc, summary = selfrun.run(self.MODS[:2], tempfile.gettempdir(), 2, out=out,
                                  run_unit=run_unit, label='full', counts={}, hold=Hold())
        self.assertEqual(rc, 0)
        self.assertEqual(log, ['unit', 'unit', 'end'])
        lines = out.getvalue().splitlines()
        self.assertLess(lines.index('kit_tests: pid hold: held to the end of the run'),
                        lines.index(summary))

    def test_units_run_in_parallel_up_to_jobs(self):
        rc, summary, out, log = self.run_fake(3)
        self.assertEqual(rc, 0, out)
        self.assertEqual(most_at_once(log), 3, log)
        self.assertEqual(len({t for _w, _m, t in log}), 6)        # a temp folder per unit
        self.assertTrue(all(not os.path.exists(t) for _w, _m, t in log))   # removed after
        self.assertRegex(summary, r'^kit_tests: OK in \d+s \(full, 3 jobs, 0 serial\): 18 tests '
                                  r'in 6 module\(s\), 3 package\(s\)$')

    def drain(self, serial=(), environ=None):
        log, lock, asks, envs = [], threading.Lock(), [], []
        pauses = dict(zip(self.MODS, (0.2, 0.2, 0.2, 0.6, 1.0, 1.4)))

        def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
            with lock:
                log.append(('start', unit[0], env['TEMP']))
                envs.append(env)
            time.sleep(pauses[unit[0]])
            with lock:
                log.append(('end', unit[0], env['TEMP']))
            return unit, 'OK', 1, pauses[unit[0]], ''

        def give_back(units):
            with lock:
                ends = sum(1 for w, _m, _t in log if w == 'end')
                asks.append((units, len(self.MODS) - ends))
        rc, _summary = selfrun.run(self.MODS, tempfile.gettempdir(), 3, serial=serial,
                                   out=io.StringIO(), run_unit=run_unit,
                                   environ=environ or {'X': '1'}, label='full', counts={},
                                   give_back=give_back)
        return rc, asks, envs

    def test_the_pool_gives_back_as_it_drains_and_only_then(self):
        rc, asks, _envs = self.drain()
        self.assertEqual(rc, 0)
        self.assertEqual([a[0] for a in asks], [3, 2, 1], asks)     # once per worker that is done
        for units, left in asks:
            self.assertLessEqual(left, units, asks)     # no unit is left that a worker must wait for

    def test_nothing_is_given_back_while_a_serial_module_waits(self):
        rc, asks, _envs = self.drain(serial=[('tests.c.test_2', 'tight timing')])
        self.assertEqual(rc, 0)
        self.assertEqual(asks, [])

    def test_units_never_get_the_offer(self):
        offer = os.path.join(tempfile.gettempdir(), 'active', 'r.giveback')
        rc, _asks, envs = self.drain(environ={'X': '1', selfrun.GIVE_ENV: offer})
        self.assertEqual(rc, 0)
        self.assertEqual(len(envs), 6)
        self.assertTrue(all(selfrun.GIVE_ENV not in e and e['X'] == '1' for e in envs), envs)

    def test_serial_units_run_after_the_pool_and_alone(self):
        serial = [('tests.a.test_2', 'shares x'), ('tests.c.test_1', 'tight timing')]
        rc, _s, out, log = self.run_fake(4, serial=serial)
        self.assertEqual(rc, 0, out)
        order = [m for w, m, _t in log if w == 'start']
        self.assertEqual(order[-2:], ['tests.a.test_2', 'tests.c.test_1'])
        last_pooled_end = max(i for i, (w, m, _t) in enumerate(log)
                              if w == 'end' and m not in dict(serial))
        first_serial_start = min(i for i, (w, m, _t) in enumerate(log)
                                 if w == 'start' and m in dict(serial))
        self.assertLess(last_pooled_end, first_serial_start, log)
        tail = log[first_serial_start:]
        self.assertEqual(most_at_once(tail), 1, tail)
        self.assertIn('kit_tests: serial tests.a.test_2: shares x', out)
        self.assertIn('4 jobs, 2 serial', out)

    def test_a_failed_unit_fails_the_run_and_shows_its_output(self):
        rc, summary, out, _log = self.run_fake(2, status={'tests.b.test_2': 'TIMEOUT'})
        self.assertEqual(rc, 1)
        self.assertIn('==== tests.b.test_2 (3 tests, 0s, TIMEOUT) ====\noutput of tests.b.test_2',
                      out)
        self.assertNotIn('output of tests.a.test_1', out)
        self.assertRegex(out, r'tests/b\s+FAILED\s+6 tests\s+2 module\(s\)')
        self.assertRegex(out, r'tests/a\s+OK\s+6 tests\s+2 module\(s\)')
        self.assertTrue(summary.startswith('kit_tests: FAILED, exit 1 in '), summary)
        self.assertTrue(summary.endswith('failed: tests.b.test_2 (TIMEOUT)'), summary)

    def test_a_runner_error_fails_its_unit_by_name(self):
        def boom(unit, *a, **kw):
            raise RuntimeError('broken')
        out = io.StringIO()
        rc, summary = selfrun.run(['tests.a.test_1'], tempfile.gettempdir(), 2, out=out,
                                  run_unit=boom, counts={})
        self.assertEqual(rc, 1)
        self.assertIn("the runner failed on this unit: RuntimeError('broken')", out.getvalue())

    def test_a_unit_folder_that_cannot_be_made_fails_that_unit(self):
        real = os.makedirs

        def makedirs(path, *a, **kw):
            if os.path.basename(path) == '1':
                raise OSError('disk full')
            return real(path, *a, **kw)
        with mock.patch.object(selfrun.os, 'makedirs', makedirs):
            rc, summary, out, _log = self.run_fake(1)
        self.assertEqual(rc, 1, out)
        self.assertIn("the runner failed on this unit: OSError('disk full')", out)
        self.assertIn(': 15 tests in 6 module(s)', summary)      # the other five ran
        self.assertRegex(summary, r'; failed: tests\.\w\.test_\d \(FAILED\)$')

    def test_ctrl_c_starts_no_new_unit_waits_for_the_running_and_exits_130(self):
        log, lock = [], threading.Lock()
        slow = fake_unit(log, lock, pause=1.5)

        def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
            if unit[0] == 'tests.a.test_1':
                threading.Timer(0.05, _thread.interrupt_main).start()   # Ctrl+C, as the console
            return slow(unit, audit, timeout, root=root, cmd=cmd, env=env)
        t = time.time()
        rc, summary, out, log = self.run_fake(2, serial=[('tests.c.test_2', 'x')],
                                              run_unit=run_unit, log=log)
        took = time.time() - t
        self.assertEqual(rc, selfrun.INTERRUPTED)
        self.assertEqual(rc, 130)
        self.assertEqual(sorted(m for w, m, _t in log if w == 'start'),
                         ['tests.a.test_1', 'tests.a.test_2'], log)    # no new unit started
        self.assertEqual(len([w for w, _m, _t in log if w == 'end']), 2, log)   # waited for
        self.assertLess(took, 3 * 1.5, log)                     # the rest were cancelled
        self.assertIn('kit_tests: interrupted (Ctrl+C)', out)
        self.assertNotIn('kit_tests: serial ', out)
        work = {os.path.dirname(t) for _w, _m, t in log}
        self.assertEqual(len(work), 1)
        self.assertFalse(os.path.exists(work.pop()))                   # the run folder is gone
        self.assertIn('kit_tests: per package', out)
        self.assertRegex(summary, r'^kit_tests: INTERRUPTED, exit 130 in \d+s \(full, 2 jobs, 1 '
                                  r'serial\): 6 tests in 2 module\(s\), 1 package\(s\); 4 '
                                  r'module\(s\) not run$')


class ReadOnlyUnitFolder(TempHomeCase):
    """A unit folder holding a temp git repository with read-only object files is removed after
    the run (kitlib.fs.rmtree.rmtree_force; its own cases: tests/fs/test_rmtree.py)."""

    def test_a_unit_folder_holding_a_read_only_repo_is_gone_after_the_run(self):
        log = []

        def run_unit(unit, audit, timeout, root=None, cmd=None, env=None):
            read_only_repo(env['TEMP'])
            log.append(env['TEMP'])
            return unit, 'OK', 1, 0.0, ''
        out = io.StringIO()
        rc, _summary = selfrun.run(['tests.a.test_1'], tempfile.gettempdir(), 1, out=out,
                                   run_unit=run_unit, environ={'X': '1'}, label='full', counts={})
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(len(log), 1)
        self.assertFalse(os.path.exists(log[0]))                # the unit's folder
        self.assertFalse(os.path.exists(os.path.dirname(log[0])))     # the run folder


KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class WorkFolder(unittest.TestCase):
    """The run folder, so every unit's TEMP, sits on the checkout's own drive."""

    def setUp(self):
        home = tempfile.mkdtemp(prefix='kit-home-')     # no machine config: this machine's
        self.addCleanup(rmtree, home)                   # temp_dir cannot steer these tests
        patch = mock.patch.object(user, 'home_dir', return_value=home)
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_parent_is_the_kits_temp_folder_for_the_checkout(self):
        """One rule with the gate logs: kitlib.config.user.work_temp (docs/test-tools.md)."""
        self.assertEqual(selfrun.work_parent(KIT), user.work_temp(KIT))
        with mock.patch.object(user, 'work_temp', return_value=os.path.join('elsewhere', 'kt')) as wt:
            self.assertEqual(selfrun.work_parent('Q:\\a\\b'), os.path.join('elsewhere', 'kt'))
        wt.assert_called_once_with('Q:\\a\\b')

    def test_the_run_folder_and_every_unit_temp_share_the_checkouts_drive(self):
        log = []
        rc, _s = selfrun.run(['tests.a.test_1', 'tests.b.test_1'], KIT, 2, out=io.StringIO(),
                             run_unit=fake_unit(log, threading.Lock(), pause=0), counts={})
        self.assertEqual(rc, 0)
        drive = os.path.normcase(os.path.splitdrive(KIT)[0])
        temps = [t for _w, _m, t in log]
        self.assertEqual(len(temps), 4, log)
        for t in temps:
            self.assertEqual(os.path.normcase(os.path.splitdrive(t)[0]), drive, t)
            self.assertEqual(os.path.normcase(os.path.dirname(os.path.dirname(t))),
                             os.path.normcase(selfrun.work_parent(KIT)), t)
            self.assertTrue(os.path.basename(os.path.dirname(t)).startswith('kit-tests-'), t)

    def test_a_drive_root_that_cannot_be_used_falls_back_with_a_note(self):
        real = os.makedirs

        def makedirs(path, *a, **kw):
            if os.path.normcase(path) == os.path.normcase(selfrun.work_parent(KIT)):
                raise OSError('access denied')
            return real(path, *a, **kw)
        log, out = [], io.StringIO()
        with mock.patch.object(selfrun.os, 'makedirs', makedirs):
            rc, _s = selfrun.run(['tests.a.test_1'], KIT, 2, out=out, counts={},
                                 run_unit=fake_unit(log, threading.Lock(), pause=0))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('could not be used (access denied)', out.getvalue())
        self.assertEqual(os.path.normcase(os.path.dirname(os.path.dirname(log[0][2]))),
                         os.path.normcase(tempfile.gettempdir()))


class Plan(unittest.TestCase):
    """plan_units: which modules split by class and the order units start in."""
    CLASSES = {'tests.a.test_big': ['One', 'Two', 'Three'], 'tests.a.test_mid': ['A', 'B'],
               'tests.b.test_small': ['S', 'T']}
    MODS = ['tests.a.test_big', 'tests.a.test_mid', 'tests.b.test_small', 'tests.b.test_new']

    def plan(self, times=None, hint=(), jobs=2):
        return selfrun.plan_units(self.MODS, tempfile.gettempdir(), jobs, self.CLASSES, times, hint)

    def test_no_record_and_no_hint_splits_nothing_and_keeps_name_order(self):
        units, split, how = self.plan()
        self.assertEqual(units, sorted(self.MODS))      # no source files here: all weigh 0
        self.assertEqual(split, {})
        self.assertIn('no recorded times', how)

    def test_an_unrecorded_hinted_module_splits_by_class_and_starts_first(self):
        units, split, _ = self.plan(hint=['tests.a.test_mid'])
        self.assertEqual(split, {'tests.a.test_mid': ['A', 'B']})
        self.assertEqual(units[:2], ['tests.a.test_mid.A', 'tests.a.test_mid.B'])
        self.assertEqual(len(units), 5)

    def test_a_module_past_half_the_ideal_wall_splits_and_units_run_longest_first(self):
        times = {'tests.a.test_big': 900.0, 'tests.a.test_mid': 100.0,
                 'tests.b.test_small': 70.0}
        # ideal wall 1070 / 2 jobs = 535; half of it 267.5: only test_big passes it
        units, split, how = self.plan(times)
        self.assertEqual(split, {'tests.a.test_big': ['One', 'Two', 'Three']})
        self.assertEqual(units[0], 'tests.b.test_new')              # unrecorded: first
        self.assertEqual(units[1:4], ['tests.a.test_big.One', 'tests.a.test_big.Three',
                                      'tests.a.test_big.Two'])      # 300 s each, then by name
        self.assertEqual(units[4:], ['tests.a.test_mid', 'tests.b.test_small'])
        self.assertIn('longest first', how)

    def test_class_records_order_the_classes_and_sum_to_the_module(self):
        times = {'tests.a.test_big.One': 50.0, 'tests.a.test_big.Two': 700.0,
                 'tests.a.test_big.Three': 150.0, 'tests.a.test_mid': 100.0,
                 'tests.b.test_small': 70.0, 'tests.b.test_new': 10.0}
        units, split, _ = self.plan(times)
        self.assertIn('tests.a.test_big', split)                    # 900 s from its classes
        self.assertEqual(units, ['tests.a.test_big.Two', 'tests.a.test_big.Three',
                                 'tests.a.test_mid', 'tests.b.test_small', 'tests.a.test_big.One',
                                 'tests.b.test_new'])

    def test_a_short_run_splits_nothing_below_the_floor(self):
        times = {m: 10.0 for m in self.MODS}
        times['tests.a.test_big'] = selfrun.SPLIT_FLOOR - 1
        _units, split, _ = self.plan(times, jobs=8)
        self.assertEqual(split, {})

    def test_a_module_that_cannot_split_runs_whole(self):
        units, split, _ = selfrun.plan_units(['tests.a.test_x'], tempfile.gettempdir(), 2, {},
                                             {'tests.a.test_x': 5000.0}, ())
        self.assertEqual((units, split), (['tests.a.test_x'], {}))

    def test_module_of(self):
        self.assertEqual(selfrun.module_of('tests.a.test_big.One'), 'tests.a.test_big')
        self.assertEqual(selfrun.module_of('tests.a.test_big'), 'tests.a.test_big')


class Times(TempHomeCase):
    def setUp(self):
        super().setUp()         # TempHomeCase: selfrun.run's run folder comes from user.make_temp
        self.dir = tempfile.mkdtemp(prefix='kit-selfrun-')
        self.addCleanup(rmtree, self.dir)
        self.path = os.path.join(self.dir, selfrun.TIMES_FILE)

    def test_missing_or_broken_records_read_as_none(self):
        self.assertEqual(selfrun.load_times(self.path), {})
        write(self.dir, {selfrun.TIMES_FILE: '{not json'})
        self.assertEqual(selfrun.load_times(self.path), {})
        self.assertEqual(selfrun.load_times(None), {})

    def test_a_run_merges_its_units_and_a_split_module_gets_its_classes_sum(self):
        write(self.dir, {selfrun.TIMES_FILE: '{"tests.z.test_other": 3.0, "tests.a.test_m": 1.0}'})
        results = [(('tests.a.test_m.A', 1, 1), 'OK', 2, 10.04, ''),
                   (('tests.a.test_m.B', 1, 1), 'FAILED', 2, 20.0, ''),
                   (('tests.b.test_t', 1, 1), 'TIMEOUT', 0, 3600.0, '')]
        selfrun.save_times(self.path, results, {'tests.a.test_m': ['A', 'B']})
        self.assertEqual(selfrun.load_times(self.path), {
            'tests.z.test_other': 3.0, 'tests.a.test_m': 30.0, 'tests.a.test_m.A': 10.0,
            'tests.a.test_m.B': 20.0})                  # a timeout is no measure of the unit
        self.assertEqual(os.listdir(self.dir), [selfrun.TIMES_FILE])     # no temp file left

    def test_a_run_that_does_not_record_still_plans_by_the_recorded_times(self):
        # kit_tests --both-tiers' real-git pass: ordered by the recorded times, writes none.
        text = '{"tests.a.test_m": 1.0, "tests.b.test_n": 50.0}'
        write(self.dir, {selfrun.TIMES_FILE: text})
        log, out = [], io.StringIO()
        mods = ['tests.a.test_m', 'tests.b.test_n']
        rc, _ = selfrun.run(mods, tempfile.gettempdir(), 1, out=out,
                            run_unit=fake_unit(log, threading.Lock(), pause=0),
                            counts={m: 3 for m in mods}, times_path=self.path,
                            record_times=False)
        self.assertEqual(rc, 0, out.getvalue())
        started = [m for w, m, _t in log if w == 'start']
        self.assertEqual(started, ['tests.b.test_n', 'tests.a.test_m'])     # longest first
        with open(self.path, encoding='utf-8') as fh:
            self.assertEqual(fh.read(), text)

    def test_a_split_run_reports_modules_not_units_and_records_times(self):
        log, out = [], io.StringIO()
        mods = ['tests.a.test_m', 'tests.b.test_n']
        rc, summary = selfrun.run(mods, tempfile.gettempdir(), 1, out=out,
                                  run_unit=fake_unit(log, threading.Lock(), pause=0),
                                  counts={m: 3 for m in mods}, classes={'tests.a.test_m': ['A', 'B']},
                                  times_path=self.path, hint=['tests.a.test_m'])
        self.assertEqual(rc, 0, out.getvalue())
        started = [m for w, m, _t in log if w == 'start']
        self.assertEqual(started, ['tests.a.test_m.A', 'tests.a.test_m.B', 'tests.b.test_n'])
        self.assertIn('kit_tests: 2 module(s) as 3 unit(s), 1 at a time', out.getvalue())
        self.assertIn('kit_tests: split by class: tests.a.test_m (2 classes)', out.getvalue())
        self.assertRegex(out.getvalue(), r'tests/a\s+OK\s+6 tests\s+1 module\(s\)')
        self.assertIn(': 9 tests in 2 module(s), 2 package(s)', summary)
        self.assertEqual(sorted(selfrun.load_times(self.path)),
                         ['tests.a.test_m', 'tests.a.test_m.A', 'tests.a.test_m.B',
                          'tests.b.test_n'])


PROBE = '''import os
import tempfile
import unittest


class T(unittest.TestCase):
    def test_own_temp(self):
        here = tempfile.gettempdir()
        self.assertEqual(os.path.normcase(here), os.path.normcase(os.environ['TEMP']))
        with open(os.path.join(os.environ['PROBE_OUT'], '%s.txt' % __name__.split('.')[-1]),
                  'w') as fh:
            fh.write(here)
'''

PID_PROBE = '''import os
import time
import unittest


class T(unittest.TestCase):
    def test_pid(self):
        with open(os.path.join(os.environ['PROBE_OUT'], __name__ + '.pid'), 'w') as fh:
            fh.write('%d %s' % (os.getpid(), os.environ['TEMP']))
'''

HOLD_UNTIL_GO = '''        deadline = time.time() + 90         # the test makes PROBE_GO; 90 s only if it never does
        while not os.path.exists(os.environ['PROBE_GO']) and time.time() < deadline:
            time.sleep(0.02)
'''

CASES = '''import unittest


class Imported(unittest.TestCase):
    def test_one(self):
        pass

    def test_two(self):
        pass
'''


class Empty(TempHomeCase):
    def setUp(self):
        super().setUp()         # TempHomeCase: selfrun.run's run folder comes from user.make_temp
        self.root = tempfile.mkdtemp(prefix='kit-selfrun-')
        self.addCleanup(rmtree, self.root)
        write(self.root, {
            'tests/__init__.py': '',
            'tests/cases.py': CASES,
            'tests/testutil.py': 'import os\n\n\ndef helper():\n    pass\n',
            'tests/test_star.py': 'from tests.cases import *  # noqa: F401,F403\n',
            'tests/test_hook.py': 'import unittest\n\nfrom tests import cases\n\n\n'
                                  'def load_tests(loader, tests, pattern):\n'
                                  '    return loader.loadTestsFromTestCase(cases.Imported)\n',
            'tests/test_broken.py': 'import no_such_module_anywhere\n',
            'tests/test_none_inherited.py': 'import unittest\n\n\nclass Base(unittest.TestCase):\n'
                                            '    pass\n',
        })

    def test_the_loader_counts_each_module_as_its_unit_will_load_it(self):
        n = selfrun.count_tests(self.root, ['tests.testutil', 'tests.test_star', 'tests.test_hook',
                                            'tests.test_broken', 'tests.test_none_inherited'])
        self.assertEqual(n['tests.testutil'], 0)
        self.assertEqual(n['tests.test_star'], 2)          # TestCases from `import *` count
        self.assertEqual(n['tests.test_hook'], 2)
        self.assertEqual(n['tests.test_none_inherited'], 0)
        self.assertNotEqual(n.get('tests.test_broken'), 0)  # unknown or a failing test: run it

    def test_the_count_child_names_the_classes_a_module_can_run_by(self):
        _n, cls = selfrun.count_units(self.root, ['tests.test_star', 'tests.test_hook',
                                                  'tests.test_broken', 'tests.test_none_inherited'])
        self.assertEqual(cls['tests.test_star'], ['Imported'])     # held by name: it can split
        self.assertNotIn('tests.test_hook', cls)                   # load_tests decides: it cannot
        self.assertNotIn('tests.test_broken', cls)
        self.assertEqual(cls['tests.test_none_inherited'], [])

    def test_a_module_with_no_test_is_listed_not_run(self):
        log, out = [], io.StringIO()
        mods = ['tests.testutil', 'tests.test_star', 'tests.test_broken']
        rc, _s = selfrun.run(mods, self.root, 2, out=out,
                             run_unit=fake_unit(log, threading.Lock(), pause=0))
        self.assertEqual(rc, 0, out.getvalue())
        self.assertEqual(sorted(m for w, m, _t in log if w == 'start'),
                         ['tests.test_broken', 'tests.test_star'])
        self.assertIn('kit_tests: (no tests) tests.testutil defines no test; not run', out.getvalue())

    def test_a_failed_count_runs_everything(self):
        with mock.patch.object(selfrun.subprocess, 'run', side_effect=OSError('no python')):
            self.assertEqual(selfrun.count_tests(self.root, ['tests.testutil']), {})


@windows_only('units run in Windows job objects')
class RealUnits(TempHomeCase):
    def setUp(self):
        super().setUp()         # TempHomeCase: the run folder (user.make_temp); units inherit it
        self.root = tempfile.mkdtemp(prefix='kit-selfrun-')
        self.addCleanup(rmtree, self.root)
        write(self.root, {'tests/__init__.py': '', 'tests/p/__init__.py': ''})

    def test_each_unit_gets_its_own_temp_folder(self):
        mods = []
        for n in range(3):
            write(self.root, {'tests/p/test_%d.py' % n: PROBE})
            mods.append('tests.p.test_%d' % n)
        probe_out = os.path.join(self.root, 'out')
        os.makedirs(probe_out)
        env = dict(os.environ, PROBE_OUT=probe_out)
        out = io.StringIO()
        rc, summary = selfrun.run(mods, self.root, 3, out=out, environ=env, timeout=120)
        self.assertEqual(rc, 0, out.getvalue())
        temps = set()
        for n in range(3):
            with open(os.path.join(probe_out, 'test_%d.txt' % n)) as fh:
                temps.add(os.path.normcase(fh.read()))
        self.assertEqual(len(temps), 3, temps)
        parent = os.path.normcase(tempfile.gettempdir())
        self.assertNotIn(parent, temps)
        self.assertIn('3 tests in 3 module(s), 1 package(s)', summary)

    def pid_units(self, n, extra=''):
        mods = []
        for i in range(n):
            write(self.root, {'tests/p/test_%d.py' % i: PID_PROBE + extra})
            mods.append('tests.p.test_%d' % i)
        probe_out = os.path.join(self.root, 'out')
        os.makedirs(probe_out, exist_ok=True)
        return mods, dict(os.environ, PROBE_OUT=probe_out)

    def probes(self):
        """{module: (pid, TEMP)} of the PID_PROBE units that ran."""
        out = os.path.join(self.root, 'out')
        found = {}
        for name in os.listdir(out):
            with open(os.path.join(out, name)) as fh:
                pid, temp = fh.read().split(' ', 1)
            found[name[:-len('.pid')]] = (int(pid), temp)
        return found

    def test_units_share_one_interpreter_per_job(self):
        mods, env = self.pid_units(6)
        out = io.StringIO()
        rc, summary = selfrun.run(mods, self.root, 2, out=out, environ=env, timeout=120)
        self.assertEqual(rc, 0, out.getvalue())
        found = self.probes()
        self.assertEqual(sorted(found), mods)
        self.assertLessEqual(len({pid for pid, _t in found.values()}), 2, found)
        self.assertEqual(len({t for _p, t in found.values()}), 6, found)   # a TEMP each still
        self.assertIn('kit_tests: interpreters: 2 pool worker(s) ran 6 unit(s); 0 unit(s) in '
                      'their own process', out.getvalue())
        self.assertIn('6 tests in 6 module(s), 1 package(s)', summary)

    def test_an_audit_hook_the_tests_package_adds_keeps_one_interpreter_per_job(self):
        # tests/__init__.py's fake-git guard is an audit hook; a worker imports the package once.
        mods, env = self.pid_units(4)
        write(self.root, {'tests/__init__.py': 'import sys\n\nsys.addaudithook(lambda e, a: None)\n'})
        out = io.StringIO()
        rc, _summary = selfrun.run(mods, self.root, 2, out=out, environ=env, timeout=120)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertLessEqual(len({pid for pid, _t in self.probes().values()}), 2)
        self.assertIn('kit_tests: interpreters: 2 pool worker(s) ran 4 unit(s); 0 unit(s) in '
                      'their own process', out.getvalue())

    def test_a_marked_module_runs_in_its_own_process(self):
        mods, env = self.pid_units(3)
        write(self.root, {'tests/p/test_0.py': PID_PROBE
                          + "\nKIT_TESTS_OWN_PROCESS = 'it patches the interpreter for good'\n"})
        out = io.StringIO()
        rc, summary = selfrun.run(mods, self.root, 1, out=out, environ=env, timeout=120)
        self.assertEqual(rc, 0, out.getvalue())
        pids = {m: pid for m, (pid, _t) in self.probes().items()}
        self.assertEqual(pids['tests.p.test_1'], pids['tests.p.test_2'])
        self.assertNotEqual(pids['tests.p.test_0'], pids['tests.p.test_1'])
        self.assertIn('kit_tests: own process: tests.p.test_0: it patches the interpreter for good',
                      out.getvalue())
        self.assertIn('kit_tests: interpreters: 1 pool worker(s) ran 2 unit(s); 1 unit(s) in '
                      'their own process', out.getvalue())

    def test_a_pooled_unit_s_process_starts_are_on_its_line_and_summed_on_the_interpreters_line(
            self):
        mods, env = self.pid_units(2)
        write(self.root, {'tests/p/test_1.py': PID_PROBE + (
            "\n    def test_starts(self):\n        import subprocess, sys\n"
            "        for _ in range(2):\n"
            "            subprocess.run([sys.executable, '-c', 'pass'], check=True)\n")})
        out = io.StringIO()
        rc, _summary = selfrun.run(mods, self.root, 1, out=out, environ=env, timeout=120)
        self.assertEqual(rc, 0, out.getvalue())
        self.assertIn('2 tests, 2 process starts  tests.p.test_1\n', out.getvalue())
        self.assertNotIn('process starts  tests.p.test_0', out.getvalue())   # it started none
        self.assertIn('kit_tests: interpreters: 1 pool worker(s) ran 2 unit(s); 0 unit(s) in '
                      'their own process; 2 process start(s) in 2 pooled unit(s) (most: '
                      'tests.p.test_1 2)\n', out.getvalue())

    def test_the_interpreters_line_names_the_heaviest_units_by_process_starts(self):
        done = [((m, 1, 1), 'OK', 1, 0.0, '') for m in ('t.a', 't.b', 't.c', 't.d', 't.e')]
        line = selfrun.interpreters_line((2, {}), done, {}, {'t.a': 4, 't.b': 9, 't.c': 0,
                                                             't.d': 4, 't.e': 1})
        self.assertEqual(line, 'kit_tests: interpreters: 2 pool worker(s) ran 5 unit(s); 0 unit(s) '
                               'in their own process; 18 process start(s) in 5 pooled unit(s) '
                               '(most: t.b 9, t.a 4, t.d 4)')
        self.assertEqual(selfrun.interpreters_line((1, {}), done[:1], {}, {'t.a': 0}),
                         'kit_tests: interpreters: 1 pool worker(s) ran 1 unit(s); 0 unit(s) in '
                         'their own process; 0 process start(s) in 1 pooled unit(s)')
        self.assertEqual(selfrun.interpreters_line((1, {}), done[:1], {}, {}),
                         'kit_tests: interpreters: 1 pool worker(s) ran 1 unit(s); 0 unit(s) in '
                         'their own process')

    def test_the_units_packages_are_their_top_level_packages(self):
        self.assertEqual(selfrun.unit_packages(['tests.p.test_0', 'tests.test_x', 'other.a.B',
                                                'top_level']), ['other', 'tests'])

    def test_the_marker_is_read_from_a_line_of_its_own(self):
        write(self.root, {'tests/p/test_m.py': "x = 1\nKIT_TESTS_OWN_PROCESS = \"why\"  # c\n",
                          'tests/p/test_n.py': "#KIT_TESTS_OWN_PROCESS = 'no'\ny = 'KIT_TESTS_"
                                               "OWN_PROCESS = 1'\n"})
        self.assertEqual(selfrun.own_process(self.root, 'tests.p.test_m'), 'why')
        self.assertIsNone(selfrun.own_process(self.root, 'tests.p.test_n'))
        self.assertIsNone(selfrun.own_process(self.root, 'tests.p.test_missing'))

    def test_ctrl_c_waits_for_the_running_units_then_ends_every_worker(self):
        from kitlib.proc import process
        mods, env = self.pid_units(4, extra=HOLD_UNTIL_GO)
        go = os.path.join(self.root, 'go')
        env['PROBE_GO'] = go
        out = io.StringIO()
        probe_out = env['PROBE_OUT']
        stop = threading.Event()
        cancelled = threading.Event()
        self.addCleanup(stop.set)
        failed = []

        class Pool(selfrun.ThreadPoolExecutor):
            def shutdown(self, *args, **kwargs):
                super().shutdown(*args, **kwargs)
                if kwargs.get('cancel_futures'):
                    cancelled.set()             # the queued units are cancelled: none can start

        def wait_for(ready, what):
            deadline = time.time() + 60
            while not stop.is_set():
                if ready():
                    return
                if time.time() > deadline:
                    failed.append('%s within 60 s: %s' % (what, os.listdir(probe_out)))
                    return
                stop.wait(0.02)

        def interrupt_then_let_the_units_end():
            # Ctrl+C, as the console, once both running units have written their probe. They hold
            # until `go` exists, made only once the run has cancelled its queued units, so no delay
            # decides whether a third unit starts: a fixed one misses on a loaded machine.
            try:
                wait_for(lambda: len(os.listdir(probe_out)) >= 2, 'fewer than 2 units wrote a probe')
                _thread.interrupt_main()        # even after a miss, so the run ends; checks fail
                wait_for(cancelled.is_set, 'the run did not cancel its queued units after Ctrl+C')
            finally:
                open(go, 'w').close()

        poller = threading.Thread(target=interrupt_then_let_the_units_end, daemon=True)
        poller.start()
        t = time.time()
        with mock.patch.object(selfrun, 'ThreadPoolExecutor', Pool):
            rc, summary = selfrun.run(mods, self.root, 2, out=out, environ=env, timeout=120)
        stop.set()
        poller.join(5)
        self.assertEqual(failed, [])
        self.assertEqual(rc, selfrun.INTERRUPTED, out.getvalue())
        self.assertLess(time.time() - t, 30)
        found = self.probes()
        self.assertEqual(len(found), 2, found)                # no new unit started
        self.assertTrue(summary.endswith('2 tests in 2 module(s), 1 package(s); 2 module(s) not '
                                         'run'), summary)
        for pid, temp in found.values():
            self.assertFalse(process.alive(pid, None), pid)   # the workers were ended
            self.assertFalse(os.path.exists(os.path.dirname(temp)))     # the run folder too

    def test_a_unit_past_its_timeout_is_killed_and_reported(self):
        write(self.root, {
            'tests/p/test_slow.py': 'import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n'
                                    '    def test_sleeps(self):\n        time.sleep(60)\n',
            'tests/p/test_quick.py': CASES,
        })
        out = io.StringIO()
        t = time.time()
        rc, summary = selfrun.run(['tests.p.test_slow', 'tests.p.test_quick'], self.root, 2,
                                  out=out, timeout=3)
        took = time.time() - t
        self.assertEqual(rc, 1, out.getvalue())
        self.assertLess(took, 40, out.getvalue())         # killed, not waited out
        self.assertIn('==== tests.p.test_slow (', out.getvalue())
        self.assertTrue(summary.endswith('failed: tests.p.test_slow (TIMEOUT)'), summary)
        self.assertIn('kit_tests: pool worker replaced after tests.p.test_slow: timeout\n',
                      out.getvalue())


if __name__ == '__main__':
    unittest.main()
