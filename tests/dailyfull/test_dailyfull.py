"""kitlib.dailyfull and tools/daily_full.py in a scratch repository with a bare remote, booking on
the scratch lock store (tests/gate/fixtures.GateRepoCase). Nothing here is scheduled."""
import datetime
import importlib.util
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from kitlib import dailyfull as D
from kitlib import gate as G
from kitlib.config import load as load_config
from kitlib.gate import runner
from kitlib.git import cmd as gitcmd
from kitlib.lock import store as lockstore
from tests.gate import fixtures

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_spec = importlib.util.spec_from_file_location('kit_daily_full_entry_for_tests',
                                               os.path.join(KIT, 'tools', 'daily_full.py'))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

FAILING = {'name': 'second', 'always': True, 'cmd': ['{python}', '-c', 'import sys; sys.exit(3)']}
MANUAL = {'name': 'by-hand', 'manual': True, 'cmd': ['{python}', '-c', 'pass']}


class GitCallHasATimeout(unittest.TestCase):
    """dailyfull's own git helper goes through kitlib.git.cmd.call with a timeout."""

    def seen(self, *args):
        calls = []

        def fake(argv, cwd, **kw):
            calls.append((list(argv), kw))
            return gitcmd.Result(0, b' out \r\n', 'err')
        with mock.patch.object(gitcmd, 'call', side_effect=fake):
            got = D.git('.', *args)
        return got, calls[0][1]

    def test_a_local_read_gets_the_kit_git_timeout(self):
        got, kw = self.seen('rev-parse', 'HEAD')
        self.assertEqual(kw['timeout'], gitcmd.GIT_TIMEOUT)
        self.assertEqual(got, (0, 'out', 'err'))

    def test_fetch_and_tree_writes_get_the_longer_one(self):
        for args in (('fetch', 'origin', '--quiet'), ('worktree', 'add', 'x'), ('switch', '--detach', 'y'),
                     ('status', '--porcelain')):
            self.assertEqual(self.seen(*args)[1]['timeout'], D.FETCH_TIMEOUT, args)
        self.assertGreater(D.FETCH_TIMEOUT, gitcmd.GIT_TIMEOUT)

    def test_a_timed_out_git_is_a_failed_step_with_the_reason(self):
        r = gitcmd.Result(None, b'', 'git fetch origin timed out after 300 seconds')
        with mock.patch.object(gitcmd, 'call', return_value=r):
            self.assertEqual(D.git('.', 'fetch', 'origin'),
                             (-1, '', 'git fetch origin timed out after 300 seconds'))

    def test_a_fetch_keeps_the_network_env(self):
        seen = []

        def fake(argv, **kw):
            seen.append(kw['env'])
            return 0, b'', b''
        with mock.patch.object(gitcmd, '_run', side_effect=fake):
            D.git('.', 'fetch', 'origin')
        self.assertEqual(seen[0]['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(seen[0]['GCM_INTERACTIVE'], 'never')


class Case(fixtures.GateRepoCase):
    KIT = {'daily_full': {'enabled': True, 'kind': 'engine-test'}}

    def setUp(self):
        fixtures.GateRepoCase.setUp(self)
        self.out = os.path.join(self.tmp, 'daily')
        self.main_dir = os.path.realpath(self.repo.root)

    def cfg(self):
        return load_config(self.main_dir)

    def run_daily(self, stamp='20261008-040000'):
        return D.run(self.main_dir, self.cfg(), out=self.out, stamp=stamp, book_kw=self.book_kw)

    def push(self, files):
        for rel, text in files.items():
            self.repo.write(rel, text)
        self.repo.commit('change', *files)
        self.repo.git('push', '-q', 'origin', 'main')

    def result(self, stamp='20261008-040000'):
        with open(os.path.join(self.out, stamp, D.RESULT), encoding='utf-8') as f:
            return json.load(f)


class Runs(Case):
    GATE = {'steps': [fixtures.GATE['steps'][0], FAILING, MANUAL]}

    def test_every_step_runs_past_a_failure_and_the_run_is_red(self):
        o = self.run_daily()
        self.assertEqual(o.code, G.FAILED, o.lines)
        r = self.result()
        self.assertEqual(r['verdict'], 'red')
        self.assertEqual(r['red'], ['second'])
        self.assertEqual([s['name'] for s in r['steps']], ['unit', 'second'])   # manual left out
        self.assertEqual(r['sha'], self.remote_main())
        self.assertTrue(os.path.isdir(os.path.join(self.main_dir, '.claude', 'worktrees',
                                                   D.SLUG)))

    def test_a_second_run_moves_the_tree_to_the_new_ref(self):
        self.run_daily('20261008-040000')
        self.push({'RED': 'x\n'})
        self.run_daily('20261009-040000')
        r = self.result('20261009-040000')
        self.assertEqual(r['sha'], self.remote_main())
        self.assertEqual(r['red'], ['unit', 'second'])


class Green(Case):
    def test_green(self):
        o = self.run_daily()
        self.assertEqual(o.code, G.OK, o.lines)
        self.assertEqual(self.result()['verdict'], 'green')
        self.assertIn('daily: GREEN at', o.lines[0])

    def test_a_default_results_folder_that_cannot_be_made_falls_back_with_a_note(self):
        """runner.make_log_dir: the shared temp's gate log base, as the gate's own logs."""
        blocker = os.path.join(self.tmp, 'a-file')
        with open(blocker, 'w') as fh:
            fh.write('x')
        shared, said = os.path.join(self.tmp, 'shared'), []
        name = runner.repo_name(self.cfg(), self.main_dir)
        with mock.patch.object(runner.kuser, 'work_temp', return_value=os.path.join(blocker, 'k')), \
                mock.patch.object(runner.kuser, 'shared_temp', return_value=shared):
            o = D.run(self.main_dir, self.cfg(), stamp='20261008-040000', say=said.append,
                      book_kw=self.book_kw)
        self.assertEqual(o.code, G.OK, o.lines)
        want = os.path.join(shared, '%s-gate' % runner.slug_of(name), 'daily', '20261008-040000')
        self.assertEqual(o.log_dir, want)
        self.assertTrue(os.path.isfile(os.path.join(want, D.RESULT)))
        self.assertTrue(any('could not be made' in s for s in said), said)


class FullCmd(Case):
    """A step whose cmd selects (and selects nothing on an unchanged tree) runs its full_cmd in a
    daily run."""
    GATE = {'steps': [{'name': 'suite', 'always': True,
                       'cmd': ['{python}', '-c', 'print("mode select")'],
                       'full_cmd': ['{python}', '-c', 'print("mode full")'], 'pass': '^mode '}]}

    def test_the_daily_run_runs_full_cmd(self):
        o = self.run_daily()
        self.assertEqual(o.code, G.OK, o.lines)
        (step,) = self.result()['steps']
        with open(step['log'], encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), 'mode full')


class DailyCmd(Case):
    """A step with a daily_cmd runs it in a daily run, ahead of its full_cmd."""
    GATE = {'steps': [{'name': 'suite', 'always': True,
                       'cmd': ['{python}', '-c', 'print("mode select")'],
                       'full_cmd': ['{python}', '-c', 'print("mode full")'],
                       'daily_cmd': ['{python}', '-c', 'print("mode daily")'], 'pass': '^mode '}]}

    def test_the_daily_run_runs_daily_cmd(self):
        o = self.run_daily()
        self.assertEqual(o.code, G.OK, o.lines)
        (step,) = self.result()['steps']
        with open(step['log'], encoding='utf-8') as f:
            self.assertEqual(f.read().strip(), 'mode daily')


class Unchanged(Case):
    def test_same_commit_after_green_is_a_recorded_skip(self):
        self.run_daily('20261008-040000')
        o = self.run_daily('20261009-040000')
        self.assertEqual(o.code, G.OK, o.lines)
        self.assertIn('daily: SKIPPED (unchanged)', o.lines[0])
        r = self.result('20261009-040000')
        self.assertEqual((r['verdict'], r['cause'], r['steps']), ('skipped', 'unchanged', []))

    def test_a_skip_does_not_hide_the_green_run_behind_it(self):
        self.run_daily('20261008-040000')
        self.run_daily('20261009-040000')
        o = self.run_daily('20261010-040000')
        self.assertIn('SKIPPED (unchanged)', o.lines[0])

    def test_the_default_results_folder_is_read_for_the_skip(self):
        """No `out`: the folder runner.make_log_dir made, the fallback included, is the one read."""
        blocker = os.path.join(self.tmp, 'a-file')
        with open(blocker, 'w') as fh:
            fh.write('x')
        shared = os.path.join(self.tmp, 'shared')
        with mock.patch.object(runner.kuser, 'work_temp', return_value=os.path.join(blocker, 'k')), \
                mock.patch.object(runner.kuser, 'shared_temp', return_value=shared):
            D.run(self.main_dir, self.cfg(), stamp='20261008-040000', book_kw=self.book_kw)
            o = D.run(self.main_dir, self.cfg(), stamp='20261009-040000', book_kw=self.book_kw)
        self.assertEqual(o.code, G.OK, o.lines)
        self.assertIn('daily: SKIPPED (unchanged)', o.lines[0])

    def test_force_runs_anyway(self):
        self.run_daily('20261008-040000')
        o = D.run(self.main_dir, self.cfg(), out=self.out, stamp='20261009-040000',
                  book_kw=self.book_kw, force=True)
        self.assertIn('daily: GREEN at', o.lines[0])

    def test_a_new_commit_runs(self):
        self.run_daily('20261008-040000')
        self.push({'NEW': 'x\n'})
        o = self.run_daily('20261009-040000')
        self.assertIn('daily: GREEN at', o.lines[0])

    def test_a_red_run_is_run_again_at_the_same_commit(self):
        self.push({'RED': 'x\n'})
        D.run(self.main_dir, self.cfg(), out=self.out, stamp='20261008-040000',
              book_kw=self.book_kw)
        with open(os.path.join(self.out, '20261008-040000', D.RESULT), encoding='utf-8') as f:
            r = json.load(f)
        r['verdict'], r['red'] = 'red', ['unit']
        with open(os.path.join(self.out, '20261008-040000', D.RESULT), 'w', encoding='utf-8') as f:
            json.dump(r, f)
        o = self.run_daily('20261009-040000')
        self.assertNotIn('unchanged', o.lines[0])

    def test_the_entry_point_has_force(self):
        out = io.StringIO()
        with redirect_stdout(out):
            tool.main(['-C', self.main_dir, '--out', self.out, '--stamp', 'a'], book_kw=self.book_kw)
            rc = tool.main(['-C', self.main_dir, '--out', self.out, '--stamp', 'b'],
                           book_kw=self.book_kw)
        self.assertEqual(rc, 0)
        self.assertIn('SKIPPED (unchanged)', out.getvalue())
        with redirect_stdout(io.StringIO()) as out2:
            tool.main(['-C', self.main_dir, '--out', self.out, '--stamp', 'c', '--force'],
                      book_kw=self.book_kw)
        self.assertIn('daily: GREEN at', out2.getvalue())

    def test_summary_calls_the_skip_calm(self):
        self.run_daily('20261008-040000')
        self.run_daily('20261009-040000')
        lines = D.summary_lines(self.out, 36)
        self.assertIn('skipped (unchanged since the last green run)', lines[0])
        self.assertIn('last ran GREEN', lines[1])


class Refusals(Case):
    def test_off_by_default_runs_nothing(self):
        self.repo.write('.kit.json', json.dumps({'gate': self.GATE}) + '\n')
        self.repo.commit('off', '.kit.json')
        o = self.run_daily()
        self.assertEqual(o.code, G.USAGE)
        self.assertIn('opt-in', o.lines[0])
        self.assertFalse(os.path.exists(self.out))

    def test_a_lock_refusal_is_a_recorded_skip(self):
        refused = G.Outcome(lockstore.EXIT_TIMEOUT, ['NOT RUN: the machine lock refused: timed out'])
        with mock.patch.object(runner, 'gated', return_value=(None, refused)):
            o = self.run_daily()
        self.assertEqual(o.code, G.OK)
        r = self.result()
        self.assertEqual((r['verdict'], r['cause']), ('skipped', 'timeout'))

    def test_an_unknown_kind_is_usage_not_a_skip(self):
        self.repo.write('.kit.json', json.dumps({'gate': self.GATE, 'daily_full': {
            'enabled': True, 'kind': 'no-such-kind'}}) + '\n')
        self.repo.commit('kind', '.kit.json')
        self.repo.git('push', '-q', 'origin', 'main')
        o = self.run_daily()
        self.assertEqual(o.code, G.USAGE, o.lines)
        self.assertEqual(self.result()['verdict'], 'red')

    def test_a_bad_ref_is_recorded_red(self):
        self.repo.write('.kit.json', json.dumps({'gate': self.GATE, 'daily_full': {
            'enabled': True, 'kind': 'engine-test', 'ref': 'origin/no-such'}}) + '\n')
        self.repo.commit('ref', '.kit.json')
        o = self.run_daily()
        self.assertEqual(o.code, G.REFUSED, o.lines)
        self.assertEqual(self.result()['red'], ['infra'])



class Trees(Case):
    """The run moves only a clean linked worktree of its own; anything else is a red `infra`
    result with the main checkout and the folder left as they were."""

    def main_head(self):
        return (self.repo.git('symbolic-ref', '-q', 'HEAD', check=False),
                self.repo.git('rev-parse', 'HEAD'))

    def refused(self, needle):
        before = self.main_head()
        o = self.run_daily()
        self.assertEqual(o.code, G.REFUSED, o.lines)
        r = self.result()
        self.assertEqual((r['verdict'], r['red']), ('red', ['infra']))
        self.assertIn(needle, r['error'])
        self.assertEqual(self.main_head(), before)
        return r

    def test_a_folder_that_is_no_worktree_is_refused(self):
        stray = os.path.join(self.main_dir, '.claude', 'worktrees', D.SLUG)
        os.makedirs(stray)
        self.repo.write('note.txt', 'mine\n', root=stray)
        self.refused('is not a worktree of its own')
        self.assertTrue(os.path.isfile(os.path.join(stray, 'note.txt')))

    def test_a_dirty_tree_is_red_and_left_alone(self):
        self.assertEqual(self.run_daily('20261007-040000').code, G.OK)
        wt = os.path.join(self.main_dir, '.claude', 'worktrees', D.SLUG)
        self.repo.write('stray.txt', 'left\n', root=wt)
        self.push({'RED': 'x\n'})
        self.refused('stray.txt')
        self.assertTrue(os.path.isfile(os.path.join(wt, 'stray.txt')))
        self.assertFalse(os.path.exists(os.path.join(wt, 'RED')))     # never moved


class TreeIsMain(Case):
    KIT = {'daily_full': {'enabled': True, 'kind': 'engine-test', 'worktree': '.'}}

    def test_worktree_dot_is_refused(self):
        before = self.repo.git('symbolic-ref', '-q', 'HEAD', check=False)
        o = self.run_daily()
        self.assertEqual(o.code, G.REFUSED, o.lines)
        r = self.result()
        self.assertEqual(r['red'], ['infra'])
        self.assertIn('is the main checkout', r['error'])
        self.assertEqual(self.repo.git('symbolic-ref', '-q', 'HEAD', check=False), before)


class AllSkipped(Case):
    GATE = {'steps': [{'name': 'needs-tool', 'always': True, 'requires': ['no-such-program-kit'],
                       'cmd': ['{python}', '-c', 'print("ok")']}]}

    def test_every_step_skipped_is_red_never_green(self):
        o = self.run_daily()
        self.assertEqual(o.code, G.FAILED, o.lines)
        r = self.result()
        self.assertEqual((r['verdict'], r['red']), ('red', ['infra']))
        self.assertEqual(r['skipped'], ['needs-tool'])
        self.assertIn('no gate step ran', r['error'])


class Entry(Case):
    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = tool.main(['-C', self.main_dir] + list(args), book_kw=self.book_kw)
        return rc, out.getvalue()

    def test_run_then_summary(self):
        rc, out = self.call('--out', self.out, '--stamp', '20261008-040000')
        self.assertEqual(rc, 0, out)
        rc, out = self.call('--summary', '--out', self.out)
        self.assertEqual(rc, 0)
        self.assertIn('daily     GREEN', out)

    def test_plan_runs_nothing(self):
        rc, out = self.call('--plan', '--out', self.out)
        self.assertEqual(rc, 0, out)
        self.assertIn('RUN   unit', out)
        self.assertFalse(os.path.exists(self.out))

    def test_help_is_never_green(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(tool.main(['--help']), G.USAGE)


class Summary(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 9, 12, 0, 0)

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix='kit-daily-')
        self.addCleanup(shutil.rmtree, self.out, True)

    def put(self, stamp, **r):
        os.makedirs(os.path.join(self.out, stamp))
        r.setdefault('finished', '%s-%s-%sT04:30:00' % (stamp[:4], stamp[4:6], stamp[6:8]))
        with open(os.path.join(self.out, stamp, D.RESULT), 'w', encoding='utf-8') as f:
            json.dump(r, f)

    def lines(self):
        return D.summary_lines(self.out, 36, now=self.NOW)

    def test_nothing_recorded(self):
        self.assertIn('no daily full run recorded yet', self.lines()[0])

    def test_red_names_the_failing_steps(self):
        self.put('20261009-040000', verdict='red', sha='a' * 40, red=['unit'],
                 steps=[{'name': 'unit', 'status': 'FAIL', 'log': 'L'}])
        lines = self.lines()
        self.assertTrue(lines[0].startswith('daily     RED aaaaaaaaaa'), lines)
        self.assertIn('failed: unit L', lines[1])

    def test_stale(self):
        self.put('20261006-040000', verdict='green', sha='b' * 40, red=[], steps=[])
        self.assertIn('STALE (over 36 h)', self.lines()[0])

    def test_skips_in_a_row_then_the_last_run(self):
        self.put('20261007-040000', verdict='green', sha='b' * 40, red=[], steps=[])
        self.put('20261008-040000', verdict='skipped', cause='busy', reason='busy')
        self.put('20261009-040000', verdict='skipped', cause='timeout', reason='timed out')
        lines = self.lines()
        self.assertIn('SKIPPED (not green: nothing ran) at 20261008-040000, 2 in a row', lines[0])
        self.assertIn('last ran GREEN', lines[1])

    def test_owner_reservation_skips_are_calm(self):
        self.put('20261009-040000', verdict='skipped', cause='reserved', reason='reserved')
        self.assertIn('skipped (owner reservation)', self.lines()[0])

    def test_unreadable_results_are_ignored(self):
        os.makedirs(os.path.join(self.out, 'junk'))
        with open(os.path.join(self.out, 'junk', D.RESULT), 'w') as f:
            f.write('{')
        self.assertIn('no daily full run recorded yet', self.lines()[0])


if __name__ == '__main__':
    unittest.main()
