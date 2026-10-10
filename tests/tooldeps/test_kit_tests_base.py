"""tools/kit_tests.py --select's base (docs/test-tools.md "kit_tests", docs/gate.md "Default
base"): origin/main when it resolves, a line when the local main differs, and an explicit --base
kept. The change list is mocked; the refs are a throwaway repository's (tests/gitfixture.py).
Also the git tier (KIT_FAKE_GIT) the units are given, and --jobs 1's pid hold (docs/test-tools.md
"The pid shield")."""
import importlib.util
import io
import os
import unittest
from unittest import mock

from tests import gitfixture

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_tests_entry_for_base_tests',
                                               os.path.join(KIT, 'tools', 'kit_tests.py'))
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)


class SelectBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = gitfixture.TempRepo(bare_remote=True)
        cls.repo.write('later.txt', 'later\n')
        cls.repo.commit('later', 'later.txt')
        cls.repo.git('push', '-q', 'origin', 'main')
        cls.repo.git('update-ref', 'refs/heads/main', cls.repo.initial)    # local main is stale

    @classmethod
    def tearDownClass(cls):
        cls.repo.cleanup()

    def run_select(self, *extra):
        bases, out = [], io.StringIO()

        def changed(base, top):
            bases.append(base)
            return [], None
        with mock.patch.object(cli.selftree, 'config', return_value={'serial': []}), \
                mock.patch.object(cli.selftree, 'changed_paths', side_effect=changed), \
                mock.patch.object(cli, 'plan', return_value=None):
            rc = cli.main(['-C', self.repo.root, '--select', '--plan'] + list(extra), out=out)
        return rc, bases, out.getvalue()

    def test_stale_local_main_selects_against_origin_main(self):
        rc, bases, out = self.run_select()
        self.assertEqual((rc, bases), (0, ['refs/remotes/origin/main']), out)
        self.assertIn('kit_tests: base: local main %s is 1 behind origin/main' %
                      self.repo.initial[:7], out)
        self.assertIn('--select against the merge-base with refs/remotes/origin/main', out)

    def test_an_explicit_base_is_kept(self):
        rc, bases, out = self.run_select('--base', 'main')
        self.assertEqual((rc, bases), (0, ['main']), out)
        self.assertNotIn('measuring against', out)

    def test_no_remote_selects_against_local_main(self):
        plain = gitfixture.TempRepo()
        self.addCleanup(plain.cleanup)
        self.assertEqual(cli.default_base(plain.root), ('main', None))


class PidHoldOptIn(unittest.TestCase):
    """--pid-hold is the opt-in: without it no hold is made; with it the hold starts before the
    units' environment is copied (its beacon reaches them, pooled or --jobs 1), and its end line
    is printed (the pool's by selfrun.run, before the summary)."""

    def run_full(self, jobs, extra=()):
        order, seen, out = [], {}, io.StringIO()

        class Hold(object):
            def start(self):
                order.append('start')
                os.environ['KIT_PID_HOLD'] = 'beacon'
                return 'pid hold: on (warmed 3 s, published)'

            def end(self):
                order.append('end')
                return 'pid hold: held to the end of the run'

        def run(modules, root, jobs, **kw):
            order.append('units')
            seen['beacon'] = kw['environ'].get('KIT_PID_HOLD')
            seen['hold'] = kw['hold']
            return 0, 'kit_tests: OK'

        def call(cmd, cwd=None, env=None):
            order.append('units')
            seen['beacon'] = env.get('KIT_PID_HOLD')
            return 0
        here = os.path.abspath('.')
        with mock.patch.dict(os.environ, {}),                 mock.patch.object(cli.unit_runner, 'PidHoldRun', Hold),                 mock.patch.object(cli.gitrepo, 'toplevel', return_value=(here, None)),                 mock.patch.object(cli.gitrepo, 'common_dir', return_value=(None, 'no git')),                 mock.patch.object(cli.selftree, 'config', return_value={'serial': []}),                 mock.patch.object(cli.selftree, 'all_test_modules', return_value=['tests.a.test_m']),                 mock.patch.object(cli.selfrun, 'run', side_effect=run),                 mock.patch.object(cli.subprocess, 'call', side_effect=call):
            os.environ.pop('KIT_PID_HOLD', None)
            rc = cli.main(['--full', '--jobs', str(jobs)] + list(extra), out=out)
        return rc, order, seen, out.getvalue()

    def test_no_hold_without_the_flag(self):
        for jobs in (1, 2):
            rc, order, seen, out = self.run_full(jobs)
            self.assertEqual((rc, order, seen.get('beacon')), (0, ['units'], None), out)
            self.assertNotIn('pid hold', out)
            if jobs == 2:
                self.assertIsNone(seen['hold'])

    def test_one_process_holds_before_its_unittest_and_reports_after(self):
        rc, order, seen, out = self.run_full(1, ['--pid-hold'])
        self.assertEqual((rc, order, seen['beacon']), (0, ['start', 'units', 'end'], 'beacon'), out)
        lines = out.splitlines()
        self.assertIn('kit_tests: pid hold: on (warmed 3 s, published)', lines)
        self.assertEqual(lines[-2:], ['kit_tests: pid hold: held to the end of the run',
                                      lines[-1]])
        self.assertTrue(lines[-1].startswith('kit_tests: OK in '), lines)

    def test_the_pool_gets_the_started_hold_and_its_beacon(self):
        rc, order, seen, out = self.run_full(2, ['--pid-hold'])
        self.assertEqual((rc, order, seen['beacon']), (0, ['start', 'units'], 'beacon'), out)
        self.assertIsNotNone(seen['hold'])


class GitTierEnvironment(unittest.TestCase):
    """The units' environment carries KIT_FAKE_GIT (a pool worker imports tests/__init__.py once,
    so its default cannot reach each unit); kit_tests' own process keeps it unchanged."""

    def run_full(self, extra=(), outer=None):
        seen, out = {}, io.StringIO()

        def run(modules, root, jobs, **kw):
            seen['pooled'] = kw['environ'].get('KIT_FAKE_GIT')
            return 0, 'kit_tests: OK'

        def call(cmd, cwd=None, env=None):
            seen['one'] = env.get('KIT_FAKE_GIT')
            return 0
        here = os.path.abspath('.')
        with mock.patch.dict(os.environ, {}), \
                mock.patch.object(cli.gitrepo, 'toplevel', return_value=(here, None)), \
                mock.patch.object(cli.gitrepo, 'common_dir', return_value=(None, 'no git')), \
                mock.patch.object(cli.selftree, 'config', return_value={'serial': []}), \
                mock.patch.object(cli.selftree, 'all_test_modules', return_value=['tests.a.test_m']), \
                mock.patch.object(cli.selfrun, 'run', side_effect=run), \
                mock.patch.object(cli.subprocess, 'call', side_effect=call):
            os.environ.pop('KIT_FAKE_GIT', None)
            if outer is not None:
                os.environ['KIT_FAKE_GIT'] = outer
            for jobs in ('2', '1'):
                self.assertEqual(cli.main(['--full', '--jobs', jobs] + list(extra), out=out), 0)
            seen['own'] = os.environ.get('KIT_FAKE_GIT')
        return seen

    def test_the_units_run_on_the_fake_by_default(self):
        self.assertEqual(self.run_full(), {'pooled': '1', 'one': '1', 'own': None})

    def test_real_git_gives_the_units_0(self):
        self.assertEqual(self.run_full(['--real-git']), {'pooled': '0', 'one': '0', 'own': None})

    def test_a_value_already_set_is_kept(self):
        self.assertEqual(self.run_full(outer='0'), {'pooled': '0', 'one': '0', 'own': '0'})


class BothTiers(unittest.TestCase):
    """--both-tiers: the pool runs twice, KIT_FAKE_GIT 1 then 0; only the last pass may give cpu
    slots back and only the fake pass records unit times; exit 0 only when both pass."""

    def run_both(self, rcs, extra=()):
        calls, out = [], io.StringIO()

        def run(modules, root, jobs, **kw):
            calls.append((kw['environ'].get('KIT_FAKE_GIT'), kw['give_back'] is not None,
                          kw['times_path'], kw.get('record_times', True)))
            kw['out'].write('kit_tests: %s in 1s (full)\n' % ('OK' if rcs[len(calls) - 1] == 0
                                                              else 'FAILED, exit 1'))
            return rcs[len(calls) - 1], ''
        here = os.path.abspath('.')
        with mock.patch.dict(os.environ, {'KIT_FAKE_GIT': '1'}), \
                mock.patch.object(cli.gitrepo, 'toplevel', return_value=(here, None)), \
                mock.patch.object(cli.gitrepo, 'common_dir', return_value=(here, None)), \
                mock.patch.object(cli.selftree, 'config', return_value={'serial': []}), \
                mock.patch.object(cli.selftree, 'all_test_modules', return_value=['tests.a.test_m']), \
                mock.patch.object(cli.giveback, 'drainer', return_value=lambda n: None), \
                mock.patch.object(cli.selfrun, 'run', side_effect=run):
            rc = cli.main(['--full', '--jobs', '2', '--both-tiers'] + list(extra), out=out)
        return rc, calls, out.getvalue()

    def test_fake_then_real_and_ok_only_when_both_pass(self):
        rc, calls, text = self.run_both([0, 0])
        self.assertEqual(rc, 0, text)
        # Both passes read the recorded times (ordering, splitting); only the fake pass records.
        times = os.path.join(os.path.abspath('.'), cli.selfrun.TIMES_FILE)
        self.assertEqual(calls, [('1', False, times, True), ('0', True, times, False)])
        lines = text.splitlines()
        self.assertEqual(lines[-1], 'kit_tests: OK, both tiers green (fake git, real git)')
        self.assertEqual([ln for ln in lines if ln.startswith('kit_tests: OK')], lines[-1:])
        self.assertIn('kit_tests [fake git]: OK in 1s (full)', lines)
        self.assertIn('kit_tests [real git]: OK in 1s (full)', lines)

    def test_a_red_real_git_pass_fails_the_run_and_prints_no_ok(self):
        rc, calls, text = self.run_both([0, 1])
        self.assertEqual(rc, 1, text)
        self.assertEqual(len(calls), 2)
        self.assertNotRegex(text, r'(?m)^kit_tests: OK')
        self.assertIn('kit_tests: FAILED, exit 1: red tier(s): real git', text)

    def test_a_red_fake_pass_still_runs_the_real_pass(self):
        rc, calls, text = self.run_both([1, 0])
        self.assertEqual((rc, len(calls)), (1, 2), text)
        self.assertIn('red tier(s): fake git\n', text)

    def test_with_real_git_or_pid_hold_is_a_usage_error(self):
        for extra in (['--real-git'], ['--pid-hold']):
            with self.assertRaises(SystemExit) as cm, \
                    mock.patch('sys.stderr', io.StringIO()):
                cli.main(['--full', '--both-tiers'] + extra, out=io.StringIO())
            self.assertEqual(cm.exception.code, cli.USAGE, extra)


if __name__ == '__main__':
    unittest.main()
