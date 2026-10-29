"""kitlib.modulemap.isolate and tools/module_map.py --isolate (docs/module-map.md "Isolation
runs"): the tree a module's test sees, the run's verdicts in real sparse clones, its timeout, its
cleanup, and its booking of the machine lock."""
import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

from kitlib.fs.rmtree import rmtree_force
from kitlib.lock.store import LockError
from kitlib.modulemap import MapError, ModuleMap
from kitlib.modulemap import isolate as I
from kitlib.proc.job import Job

from tests.gitfixture import TempRepo
from tests.realgit import real_git

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import module_map as cli  # noqa: E402

# A test command: exits 0 when every path given exists (a path with a leading ! must not), 3
# otherwise; `--sleep S` first sleeps S seconds.
CHECK_PY = '\n'.join([
    'import os, sys, time',
    'args = sys.argv[1:]',
    'if args[:1] == ["--sleep"]:',
    '    time.sleep(float(args[1]))',
    '    args = args[2:]',
    'bad = [a for a in args if os.path.exists(a.lstrip("!")) == a.startswith("!")]',
    'print("checked %d path(s); wrong: %s" % (len(args), bad))',
    'sys.exit(3 if bad else 0)',
    ''])

FILES = {'conf.txt': 'top\n', 'tools/helper.txt': 'root folder\n', 'app/check.py': CHECK_PY,
         'lib/core.txt': 'lib\n', 'lib/inner/i.txt': 'inner\n', 'proto/p.txt': 'messaging\n',
         'other/o.txt': 'other\n', 'data/d.txt': 'data\n', 'deep/z.txt': 'deep\n'}

MODULES = {'app': ['app'], 'lib': ['lib'], 'inner': ['lib/inner'], 'proto': ['proto'],
           'other': ['other'], 'data': ['data'], 'deep': ['deep']}


def edge(src, dst, kind='build'):
    return {'from': src, 'to': dst, 'kind': kind, 'why': 'test'}


def a_map(test=None, edges=(), ratchets=(), include=None, tests=None):
    """A ModuleMap over MODULES: app's test is `test` (argv after {python} {root}/app/check.py),
    or `tests` {module: test entry} gives them all."""
    mods = {n: {'paths': list(p)} for n, p in MODULES.items()}
    if test is not None:
        mods['app']['test'] = {'cmd': ['{python}', '{root}/app/check.py'] + list(test)}
    for n, t in (tests or {}).items():
        mods[n]['test'] = t
    data = {'modules': mods, 'messaging': ['proto'], 'edges': list(edges),
            'ratchets': list(ratchets)}
    if include is not None:
        data['isolate'] = {'include': include}
    return ModuleMap(data)


class Schema(unittest.TestCase):
    def test_a_test_and_isolate_include_load_with_their_defaults(self):
        mm = a_map(['conf.txt'], include=['tools'])
        self.assertEqual(I.test_of(mm, 'app'), {
            'cmd': ['{python}', '{root}/app/check.py', 'conf.txt'], 'cwd': '', 'timeout': 1800})
        self.assertEqual(mm.isolate_include, ['tools'])

    def test_a_timeout_past_the_cap_or_a_cmd_field_it_does_not_fill_is_refused(self):
        for test, want in (({'cmd': ['x'], 'timeout': 14401}, 'timeout'),
                           ({'cmd': ['x'], 'timeout': 0}, 'timeout'),
                           ({'cmd': ['{python}', '{tree}']}, '{tree}'),
                           ({'cmd': []}, 'cmd'),
                           ({'cmd': ['x'], 'cwd': '../up'}, 'cwd'),
                           ({'cmd': ['x'], 'shell': True}, 'shell')):
            with self.subTest(test=test), self.assertRaisesRegex(MapError, want):
                a_map(tests={'app': test})

    def test_an_include_must_be_held_by_the_root_and_named_without_a_glob(self):
        with self.assertRaisesRegex(MapError, r'isolate.include lib/x is held by module lib'):
            a_map(include=['lib/x'])
        with self.assertRaisesRegex(MapError, 'glob'):
            a_map(include=['tool*'])
        a_map(include=['tools', 'conf.txt'])


class Tree(unittest.TestCase):
    def test_the_tree_holds_the_module_messaging_and_every_direct_edge_and_ratchet(self):
        mm = a_map(edges=[edge('app', 'lib', 'data'), edge('lib', 'deep', 'data')],
                   ratchets=[{'id': 'r1', 'from': 'app', 'to': 'other', 'kinds': ['source'],
                              'goal': 0}])
        # app's own data edge and source ratchet count; lib's data edge does not.
        self.assertEqual(I.members(mm, 'app'), {'app', 'proto', 'lib', 'other'})

    def test_build_edges_and_build_ratchets_are_followed_from_every_member(self):
        mm = a_map(edges=[edge('app', 'lib'), edge('lib', 'data'), edge('proto', 'deep')],
                   ratchets=[{'id': 'r1', 'from': 'data', 'to': 'other', 'goal': 0},
                             {'id': 'r2', 'from': 'data', 'to': 'inner', 'kinds': ['data'],
                              'goal': 0}])
        self.assertEqual(I.members(mm, 'app'),
                         {'app', 'proto', 'lib', 'data', 'deep', 'other'})

    def test_the_root_files_are_the_top_level_ones_and_the_includes(self):
        mm = a_map(edges=[edge('app', 'lib')], include=['tools'])
        self.assertEqual(I.patterns(mm, I.members(mm, 'app')), [
            '/*', '!/*/', '/app', '!/data', '!/deep', '/lib', '!/other', '/proto', '/tools',
            '!/lib/inner'])

    def test_an_edge_to_the_root_holds_all_of_it_but_the_modules_outside_the_tree(self):
        mm = a_map(edges=[edge('app', '(root)', 'data'), edge('app', 'inner')])
        self.assertEqual(I.patterns(mm, I.members(mm, 'app')), [
            '/*', '/app', '!/data', '!/deep', '!/lib', '!/other', '/proto', '/lib/inner'])

    def test_the_plan_names_each_tree_and_the_modules_with_no_test(self):
        mm = a_map(['conf.txt'], edges=[edge('app', 'lib')])
        lines = I.plan_lines(mm, I.plan(mm))
        self.assertEqual(lines[0], 'module_map isolate: app: holds app, lib, proto; runs {python} '
                                   '{root}/app/check.py conf.txt in the tree root; timeout 1800s')
        self.assertTrue(lines[1].startswith('module_map isolate: app: sparse patterns: /* !/*/'))
        self.assertEqual(lines[2], 'module_map isolate: not isolated (no test): data, deep, '
                                   'inner, lib, other, proto')

    def test_only_names_a_module_that_has_a_test(self):
        mm = a_map(['conf.txt'])
        self.assertEqual([e.module for e in I.plan(mm, 'app')], ['app'])
        with self.assertRaisesRegex(MapError, 'no such module'):
            I.plan(mm, 'nope')
        with self.assertRaisesRegex(MapError, 'has no test'):
            I.plan(mm, 'lib')


class JobSlot(object):
    """A stand-in for the machine-lock Slot: its commands start in one kill-on-close job, and
    end_tree ends every process in it."""

    def __init__(self, stop_after=None):
        self.job = Job()
        self.env = dict(os.environ)
        self.ended = 0
        self.stop_after = stop_after        # stop_asked is True from this call on
        self.asked = 0

    def start(self, cmd, **kw):
        return self.job.start(cmd, **kw)

    def end_tree(self):
        self.ended += 1
        self.job.terminate()

    def stop_asked(self):
        self.asked += 1
        return self.stop_after is not None and self.asked >= self.stop_after

    def close(self):
        self.job.close()


class RunTest(unittest.TestCase):
    """run_test: a test past its timeout, or stopped by the lock, is ended with its tree."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='kit-iso-test-')
        self.addCleanup(rmtree_force, self.dir, True)
        self.slot = JobSlot()
        self.addCleanup(self.slot.close)
        self.log = os.path.join(self.dir, 'out.log')

    def sleeper(self, seconds):
        return [sys.executable, '-c', 'import time; print("up", flush=True); time.sleep(%d)'
                % seconds]

    def test_a_test_past_its_timeout_is_ended_with_its_tree(self):
        start = time.monotonic()
        got = I.run_test(self.slot, self.sleeper(60), self.dir, dict(os.environ), self.log, 2,
                         poll=0.2)
        self.assertEqual((got.code, got.how), (None, 'timeout'))
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(self.slot.ended, 1)
        self.assertEqual(self.slot.job.pids(), [])

    def test_a_stop_of_the_lock_ends_the_test(self):
        self.slot.stop_after = 1
        got = I.run_test(self.slot, self.sleeper(60), self.dir, dict(os.environ), self.log, 600,
                         poll=0.2)
        self.assertEqual(got.how, 'stopped')
        self.assertEqual(self.slot.ended, 1)

    def test_an_exit_code_and_the_output_are_kept(self):
        got = I.run_test(self.slot, [sys.executable, '-c', 'print("hi"); raise SystemExit(4)'],
                         self.dir, dict(os.environ), self.log, 60, poll=0.2)
        self.assertEqual((got.code, got.how), (4, 'done'))
        with open(self.log, encoding='utf-8') as fh:
            self.assertEqual(fh.read().strip(), 'hi')
        self.assertEqual(self.slot.ended, 0)

    def test_a_folder_outside_the_tree_is_an_error_and_nothing_starts(self):
        got = I.run_test(self.slot, ['never'], os.path.join(self.dir, 'none'), {}, self.log, 60)
        self.assertEqual(got.how, 'error')
        self.assertIn('is not in the tree', got.note)

    def test_the_echo_keeps_the_last_lines(self):
        with open(self.log, 'w', encoding='utf-8') as fh:
            fh.write(''.join('line %d\n' % i for i in range(10)))
        said = []
        I.echo(said.append, 'app', 'isolated', self.log, tail=3)
        self.assertEqual(said, [
            'module_map isolate: --- app, isolated run: output (the last 3 of 10 lines) ---',
            'line 7', 'line 8', 'line 9', 'module_map isolate: --- end of app, isolated run ---'])


def _prepare(repo):
    for rel, text in FILES.items():
        repo.write(rel, text)
    repo.commit('the modules', *FILES)


def _git_state(repo):
    """What a run must leave as it was: the source's config, refs and worktree list."""
    with open(os.path.join(repo.common_dir(), 'config'), 'rb') as fh:
        config = fh.read()
    return config, repo.git('for-each-ref'), repo.git('worktree', 'list', '--porcelain')


@real_git('sparse checkout and clone --shared --config: the fake has neither')
class IsolationRun(unittest.TestCase):
    """run() in real sparse clones of a fixture repository."""

    def setUp(self):
        self.repo = TempRepo(prepare=_prepare, prepare_key='modulemap-isolate')
        self.addCleanup(self.repo.cleanup)
        self.parent = tempfile.mkdtemp(prefix='kit-iso-parent-')
        self.addCleanup(rmtree_force, self.parent, True)
        patch = mock.patch.object(
            I.kuser, 'make_temp',
            lambda near, prefix: (tempfile.mkdtemp(prefix=prefix, dir=self.parent), None))
        patch.start()
        self.addCleanup(patch.stop)
        self.slot = JobSlot()
        self.addCleanup(self.slot.close)
        self.said = []

    def run_map(self, mm, **kw):
        kw.setdefault('poll', 0.2)
        return I.run(self.repo.root, mm, self.slot, self.said.append, **kw)

    def verdicts(self):
        return [line for line in self.said
                if line.startswith('module_map isolate: app ') or 'isolate: OK' in line
                or 'isolate: FAILED' in line]

    def test_a_module_whose_edges_reach_what_it_uses_passes_in_a_tree_holding_only_those(self):
        mm = a_map(['conf.txt', 'lib/core.txt', 'proto/p.txt', '!lib/inner/i.txt',
                    '!other/o.txt', '!tools/helper.txt', '!data/d.txt'],
                   edges=[edge('app', 'lib')])
        self.assertEqual(self.run_map(mm), 0, '\n'.join(self.said))
        self.assertRegex(self.said[-2], r'^module_map isolate: app PASS \(exit 0, \d+s\)$')
        self.assertEqual(self.said[-1], 'module_map isolate: OK: 1 PASS')

    def test_a_module_using_what_its_map_entry_does_not_allow_is_coupled(self):
        mm = a_map(['lib/core.txt'])            # no edge from app to lib
        self.assertEqual(self.run_map(mm), 1)
        out = '\n'.join(self.said)
        self.assertIn("wrong: ['lib/core.txt']", out)            # the isolated run's output
        self.assertIn('module_map isolate: app COUPLED: it passes in a full clone, so it needs '
                      'something its tree leaves out (isolated: exit 3, ', out)
        self.assertIn('; its tree holds app, proto)', out)
        self.assertEqual(self.said[-1], 'module_map isolate: FAILED: 1 COUPLED (app)')

    def test_a_module_whose_tests_fail_anyway_is_red(self):
        mm = a_map(['no-such-file'])
        self.assertEqual(self.run_map(mm), 1)
        self.assertTrue(any(line.startswith('module_map isolate: app RED: it fails in a full '
                                            'clone too (isolated: exit 3, ') for line in self.said),
                        '\n'.join(self.said))
        self.assertEqual(self.said[-1], 'module_map isolate: FAILED: 1 RED (app)')

    def test_without_the_rerun_a_failing_module_is_not_told_coupled_or_red(self):
        mm = a_map(['lib/core.txt'])
        self.assertEqual(self.run_map(mm, rerun=False), 1)
        self.assertIn('not run again in a full clone', '\n'.join(self.said))
        self.assertEqual(self.said[-1], 'module_map isolate: FAILED: 1 FAIL (app)')

    def test_a_test_past_its_timeout_in_both_trees_is_timeout(self):
        mm = a_map(tests={'app': {'cmd': ['{python}', 'app/check.py', '--sleep', '60'],
                                  'timeout': 2}})
        start = time.monotonic()
        self.assertEqual(self.run_map(mm), 1)
        self.assertLess(time.monotonic() - start, 90)
        self.assertIn('module_map isolate: app TIMEOUT: it times out in a full clone too '
                      '(isolated: timed out after 2s, ended with its tree; full clone: timed out '
                      'after 2s, ended with its tree)', self.said)
        self.assertEqual(self.slot.ended, 2)

    def test_an_include_adds_a_root_folder_and_cwd_and_module_are_filled_in(self):
        mm = a_map(tests={'app': {'cmd': ['{python}', 'check.py', '../tools/helper.txt',
                                          '{module}.marker-absent-ok', '!../lib/core.txt'],
                                  'cwd': 'app'}}, include=['tools'])
        rc = self.run_map(mm)
        # {module} is app: app/app.marker-absent-ok does not exist, so the check fails on it
        # alone, and it fails the same in the full clone: RED, with the include seen.
        self.assertEqual(rc, 1)
        out = '\n'.join(self.said)
        self.assertIn("wrong: ['app.marker-absent-ok']", out)
        mm = a_map(tests={'app': {'cmd': ['{python}', 'check.py', '../tools/helper.txt',
                                          '!../lib/core.txt'], 'cwd': 'app'}}, include=['tools'])
        self.said = []
        self.assertEqual(self.run_map(mm), 0, '\n'.join(self.said))

    def test_the_clones_and_the_run_folder_go_and_the_source_is_left_as_it_was(self):
        before = _git_state(self.repo)
        status = self.repo.git('status', '--porcelain')
        self.assertEqual(self.run_map(a_map(['lib/core.txt'])), 1)     # COUPLED: two clones
        self.assertEqual(os.listdir(self.parent), [])
        self.assertEqual(_git_state(self.repo), before)
        self.assertEqual(self.repo.git('status', '--porcelain'), status)

    def test_only_committed_head_is_isolated(self):
        self.repo.write('lib/core.txt', 'changed in the work tree\n')
        self.repo.write('new.txt', 'untracked\n')
        mm = a_map(['!new.txt', 'lib/core.txt'], edges=[edge('app', 'lib')])
        self.assertEqual(self.run_map(mm), 0, '\n'.join(self.said))


class Sweep(unittest.TestCase):
    def setUp(self):
        self.parent = tempfile.mkdtemp(prefix='kit-iso-sweep-')
        self.addCleanup(rmtree_force, self.parent, True)

    def test_a_run_folder_whose_process_is_gone_is_removed_and_a_live_one_is_kept(self):
        names = ['kit-iso-111-222-aa', 'kit-iso-333-x-bb', 'kit-iso-444-555-cc', 'other-111-222']
        for n in names:
            os.makedirs(os.path.join(self.parent, n, 'repo'))
        live = {(444, 555)}
        gone = I.sweep(self.parent, alive=lambda pid, ctime: (pid, ctime) in live)
        self.assertEqual(gone, 2)
        self.assertEqual(sorted(os.listdir(self.parent)), ['kit-iso-444-555-cc', 'other-111-222'])

    def test_the_runs_own_folder_is_kept(self):
        own = os.path.join(self.parent, 'kit-iso-1-2-me')
        os.makedirs(own)
        self.assertEqual(I.sweep(self.parent, keep=own, alive=lambda pid, ctime: False), 0)
        self.assertTrue(os.path.isdir(own))


class FakeCfg(object):
    def __init__(self, kind='daily-full', max_wait=7200, name='demo'):
        self.repo = {'name': name}
        self.daily = {'kind': kind, 'max_wait': max_wait}

    def section(self, name):
        assert name == 'daily_full', name
        return self.daily


class Booking(unittest.TestCase):
    def test_a_plan_books_nothing(self):
        said = []

        def book(*a, **kw):
            raise AssertionError('a plan booked the lock')
        rc = I.run_tool('C:\\nowhere', FakeCfg(), a_map(['conf.txt']), said.append,
                        plan_only=True, book=book)
        self.assertEqual(rc, 0)
        self.assertTrue(said[0].startswith('module_map isolate: app: holds app, proto; runs'))

    def test_the_run_books_the_daily_full_kind_and_waits_its_max_wait(self):
        seen = []

        def book(kind, repo, slug, **kw):
            seen.append((kind, repo, slug, kw['wait_s']))
            raise LockError(8, 'timed out waiting')
        said = []
        rc = I.run_tool('C:\\nowhere', FakeCfg('nightly', 900), a_map(['conf.txt']),
                        said.append, book=book)
        self.assertEqual(seen, [('nightly', 'demo', 'module-isolation', 900)])
        self.assertEqual(rc, 2)
        self.assertEqual(said, ['module_map isolate: NOT RUN: the machine lock refused the run '
                                '(exit 8): timed out waiting'])

    def test_no_test_in_the_map_books_nothing_and_passes(self):
        said = []
        rc = I.run_tool('C:\\nowhere', FakeCfg(), a_map(), said.append,
                        book=lambda *a, **kw: self.fail('booked'))
        self.assertEqual((rc, said), (0, ['module_map isolate: no module in the map names a '
                                          'test; nothing to run']))


class Cli(unittest.TestCase):
    """The tool's arguments and exit codes."""

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue()

    def test_the_isolate_options_go_with_isolate_and_map_only_under_plan(self):
        for argv, want in ((['--check', '--plan'], '--module, --plan and --no-rerun go with '
                                                   '--isolate'),
                           (['--scope', '--module', 'x'], 'go with --isolate'),
                           (['--isolate', '--map', 'm.json'], '--map goes with it only under '
                                                              '--plan'),
                           (['--isolate', '--plan', '--ceilings', 'c.json'], '--ceilings')):
            with self.subTest(argv=argv):
                code, out = self.run_cli('-C', KIT_ROOT, *argv)
                self.assertEqual(code, 2)
                self.assertIn(want, out)

    def test_a_plan_from_a_given_map_is_a_dry_run(self):
        d = tempfile.mkdtemp(prefix='kit-iso-cli-')
        self.addCleanup(rmtree_force, d, True)
        path = os.path.join(d, 'map.json')
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump({'modules': {'app': {'paths': ['app'], 'test': {'cmd': ['{python}', 't']}}}},
                      fh)
        code, out = self.run_cli('-C', KIT_ROOT, '--isolate', '--plan', '--map', path)
        self.assertEqual(code, 0, out)
        self.assertIn('module_map: dry run: the map was given on the command line', out)
        self.assertIn('module_map isolate: app: holds app; runs {python} t in the tree root', out)
        code, out = self.run_cli('-C', KIT_ROOT, '--isolate', '--plan', '--map', path,
                                 '--module', 'lib')
        self.assertEqual(code, 2)
        self.assertIn('module_map: NOT CHECKED: --module lib: the map has no such module', out)


if __name__ == '__main__':
    unittest.main()
