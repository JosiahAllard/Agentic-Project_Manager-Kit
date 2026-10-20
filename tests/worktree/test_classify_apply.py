"""kitlib.worktree.classify with apply (`worktree_remove.py --classify --apply`): A and E trees go
through the existing remove paths (plain, --scratch, --abandon "evidence"), B, C and L are never
touched, a held landing lock stops the run, and those paths' own last look refuses a tree that
turned live after it was classified. Throwaway repositories only (tests/gitfixture.py).
"""
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from kitlib.worktree import classify, remove, sweep
from tests import gitfixture
from tests.realgit import real_git
from tests.worktree.test_classify import LATER, ClassifyCase, free, no_procs
from tests.worktree.test_worktree_remove import TOOL, WINDOWS


def load_tool():
    spec = importlib.util.spec_from_file_location('worktree_remove_tool', TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@real_git('git reflog: the fake writes no reflog, and A and --scratch read it')
@gitfixture.real_fixture('A reads the reflog entry a fixture commit() writes, which only real '
                         'git writes')
class ApplyCase(ClassifyCase):

    def setUp(self):
        super().setUp()
        self.temp = os.path.join(os.path.realpath(self.fx.base), 'outside')
        os.makedirs(self.temp, exist_ok=True)

    def run_apply(self, top=None, procs=no_procs, machine=free):
        got = classify.classify(top or self.repo, now=time.time() + LATER, machine=machine,
                                procs=procs, apply=True, environ={'AGENT_TEMP': self.temp})
        return got.code, '\n'.join(got.lines), got.data

    def gone(self, path):
        self.assertFalse(os.path.exists(path), path)
        self.assertNotIn(os.path.normcase(path), os.path.normcase(
            self.git('worktree', 'list', '--porcelain')))

    def kept(self, path):
        self.assertTrue(os.path.isdir(path), path)
        self.assertIn(os.path.normcase(path.replace('\\', '/')), os.path.normcase(
            self.git('worktree', 'list', '--porcelain').replace('\\', '/')))


class Removes(ApplyCase):

    def test_a_landed_tree_is_removed_and_its_branch_deleted(self):
        path = self.workstream('done')
        self.land('kit/done')
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s: A, ' % path, out)
        self.gone(path)
        self.assertNotIn('kit/done', self.branches())
        self.assertEqual(self.row(data, path)['apply']['result'], 'removed')
        self.assertEqual(data['applied'], {'removed': 1, 'failed': 0, 'skipped': 0})
        self.assertIn('applied   removed 1, failed 0, skipped 0', out)

    def test_an_evidence_tree_with_unlanded_commits_is_abandoned_under_a_tag(self):
        path = self.workstream('agent-7')
        tip = self.git('rev-parse', 'HEAD', cwd=path)
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s: E, --abandon "evidence"' % path, out)
        self.assertIn('archive/agent-7', out)
        self.gone(path)
        self.assertEqual(self.git('rev-parse', 'refs/tags/archive/agent-7^{commit}'), tip)
        self.assertIn('abandoned: evidence',
                      self.git('tag', '-l', '--format=%(contents)', 'archive/agent-7'))

    def test_a_detached_scratch_tree_is_removed_through_scratch(self):
        path = self.outside('scratch', self.git('rev-parse', 'HEAD'))
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s: E, --scratch' % path, out)
        self.gone(path)
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')

    def test_a_landed_evidence_tree_is_removed_plainly(self):
        path = self.workstream('agent-2', commits=0)
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s: E, plain remove' % path, out)
        self.gone(path)
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')


class Untouched(ApplyCase):

    def test_b_c_and_l_trees_are_left_on_disk(self):
        done = self.workstream('done')
        self.land('kit/done')
        ready = self.workstream('ready')
        self.green(ready)
        wip = self.workstream('wip')
        self.fx.write('loose.txt', 'x\n', root=wip)
        kept = self.workstream('kept')
        self.land('kit/kept')
        self.git('worktree', 'lock', '--reason', 'owner keeps it', kept)
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        self.gone(done)
        for path in (ready, wip, kept):
            self.kept(path)
            self.assertNotIn('apply', self.row(data, path))
            self.assertNotIn(path + ':', out)
        self.assertTrue(os.path.isfile(os.path.join(wip, 'loose.txt')))
        self.assertIn('B finished', out)
        self.assertIn('C half-done', out)
        self.assertIn('L live/kept', out)
        self.assertNotIn('A landed', out)
        self.assertIn('next      ', out)
        self.assertEqual(data['applied'], {'removed': 1, 'failed': 0, 'skipped': 0})

    def test_the_applied_lines_come_before_the_b_c_l_summary(self):
        done = self.workstream('done')
        self.land('kit/done')
        self.workstream('wip')
        rc, out, data = self.run_apply()
        self.assertLess(out.index('removed   %s' % done), out.index('C half-done'))
        self.assertLess(out.index('C half-done'), out.index('summary   '))

    def test_a_held_landing_lock_removes_nothing(self):
        done = self.workstream('done')
        self.land('kit/done')
        self.workstream('agent-7')
        with open(os.path.join(self.fx.common_dir(), 'kit-landing.lock'), 'w') as f:
            json.dump({'lane': 'full', 'branches': ['kit/other'], 'token': 't'}, f)
        before = self.snapshot()
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        self.assertIn('refused   ', out)
        self.assertIn('is held', out)
        self.assertIn('nothing was classified or removed', out)
        self.assertEqual(self.snapshot(), before)
        self.kept(done)

    def test_a_landing_lock_taken_mid_run_skips_the_rest(self):
        first = self.workstream('done')
        self.land('kit/done')
        second = self.workstream('agent-7')
        holds = iter([None, None, 'the landing lock X is held (test)'])
        with mock.patch.object(sweep, 'landing_hold', lambda ctx: next(holds)):
            rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        results = {p: self.row(data, p)['apply']['result'] for p in (first, second)}
        self.assertEqual(sorted(results.values()), ['removed', 'skipped'], out)
        skipped = next(p for p, r in results.items() if r == 'skipped')
        self.gone(next(p for p, r in results.items() if r == 'removed'))
        self.kept(skipped)
        self.assertIn('skipped   %s: the landing lock X is held (test)' % skipped, out)
        self.assertEqual(data['applied'], {'removed': 1, 'failed': 0, 'skipped': 1})

    def test_apply_from_a_worktree_is_refused(self):
        done = self.workstream('done')
        self.land('kit/done')
        other = self.workstream('other')
        rc, out, data = self.run_apply(top=other)
        self.assertEqual(rc, 1, out)
        self.assertIn('main checkout only', out)
        self.kept(done)

    @unittest.skipUnless(WINDOWS, 'holders.blockers opens folders for delete: Windows only')
    def test_a_holder_the_process_seam_misses_is_refused_by_the_remove_path(self):
        # The process seam reports nobody, so classify judges the tree A; the remove path's own
        # holder check (an open for delete) finds the real process and refuses.
        path = self.workstream('done')
        self.land('kit/done')
        inner = os.path.join(path, 'sub')
        os.makedirs(inner)
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                                 cwd=inner)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.row(data, path)['class'], 'A')
        self.assertIn('failed    %s: ' % path, out)
        self.assertIn('is held', self.row(data, path)['apply']['why'])
        self.kept(path)
        self.assertIn('kit/done', self.branches())

    def test_a_tree_dirtied_after_it_was_classified_is_refused_by_the_remove_path(self):
        path = self.workstream('done')
        self.land('kit/done')
        judge = classify.judge

        def judged_then_dirtied(ctx, entry, *rest):
            got = judge(ctx, entry, *rest)
            self.fx.write('late.txt', 'written after the judgement\n', root=entry['path'])
            return got
        with mock.patch.object(classify, 'judge', judged_then_dirtied):
            rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    %s: ' % path, out)
        self.kept(path)
        self.assertTrue(os.path.isfile(os.path.join(path, 'late.txt')))


class Unsaved(ApplyCase):

    def test_an_evidence_tree_whose_reflog_holds_an_unsaved_commit_is_skipped(self):
        # A detached agent-* tree commits X, then checks out another commit: only its HEAD reflog
        # holds X, and `git worktree remove` would delete that reflog with the tree.
        path = self.workstream('agent-8')
        self.git('checkout', '-q', '--detach', cwd=path)
        self.fx.write('x.txt', 'x\n', root=path)
        lost = self.fx.commit('x', 'x.txt', cwd=path)
        self.git('checkout', '-q', '--detach', 'HEAD~1', cwd=path)
        self.git('branch', '-D', 'kit/agent-8')
        rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        row = self.row(data, path)
        self.assertEqual(row['apply']['result'], 'skipped', out)
        self.assertIn('reflog holds unsaved commits', row['apply']['why'])
        self.assertIn('skipped   %s: ' % path, out)
        self.kept(path)
        self.assertEqual(self.git('tag', '-l', 'archive/*'), '')
        self.assertIn(lost, self.git('reflog', 'show', '--format=%H', 'HEAD', cwd=path))


class Outcomes(ApplyCase):

    def test_a_removed_tree_whose_branch_delete_was_refused_counts_as_failed(self):
        path = self.workstream('done')
        self.land('kit/done')

        def refused(ctx, branch, keep, removed=True):
            ctx.say('failed    git branch -d %s exited 1: faked' % branch)
            return remove.REFUSED
        with mock.patch.object(remove, 'delete_branch', refused):
            rc, out, data = self.run_apply()
        self.assertEqual(rc, 1, out)
        self.gone(path)
        row = self.row(data, path)
        self.assertEqual(row['apply']['result'], 'failed', out)
        self.assertIn('the worktree was removed, but', row['apply']['why'])
        self.assertEqual(data['applied'], {'removed': 0, 'failed': 1, 'skipped': 0})

    def test_a_machine_lock_taken_mid_run_skips_that_tree(self):
        path = self.workstream('done')
        self.land('kit/done')
        calls = []

        def machine():
            calls.append(1)
            if len(calls) == 1:
                return [], None
            return [{'repo': 'r', 'slug': 'x', 'id': 7,
                     'note': 'training in .claude/worktrees/done'}], None
        rc, out, data = self.run_apply(machine=machine)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.row(data, path)['class'], 'A')
        row = self.row(data, path)
        self.assertEqual(row['apply']['result'], 'skipped', out)
        self.assertTrue(row['apply']['why'].startswith('machine lock: r/x run 7'), row)
        self.kept(path)

    def test_a_machine_lock_unreadable_mid_run_skips_that_tree(self):
        path = self.workstream('done')
        self.land('kit/done')
        answers = iter([([], None), (None, 'tool gone')])
        rc, out, data = self.run_apply(machine=lambda: next(answers))
        self.assertEqual(rc, 1, out)
        row = self.row(data, path)
        self.assertEqual(row['apply']['result'], 'skipped', out)
        self.assertIn('the machine lock cannot be read (tool gone)', row['apply']['why'])
        self.kept(path)


class Arguments(unittest.TestCase):

    def setUp(self):
        self.tool = load_tool()

    def test_classify_takes_apply_and_json(self):
        self.assertEqual(self.tool.parse(['--classify', '--apply']),
                         ('classify-apply', None, None, False, False))
        self.assertEqual(self.tool.parse(['--apply', '--json', '--classify']),
                         ('classify-apply', None, None, False, True))
        self.assertEqual(self.tool.parse(['--classify']), ('classify', None, None, False, False))

    def test_apply_without_classify_still_means_the_sweep(self):
        self.assertEqual(self.tool.parse(['--sweep-merged', '--apply']),
                         ('sweep', None, None, False, True))
        self.assertEqual(self.tool.parse(['--apply']), '--sweep-merged takes only --apply (and -C)')

    def test_argument_errors_give_the_usage_code(self):
        for args in (['--classify', '--apply', '--sweep-merged'], ['--classify', '--apply', 'x'],
                     ['--classify', '--apply', '--apply'], ['--json', '--apply']):
            with mock.patch.object(sys, 'stderr'):
                self.assertEqual(self.tool.main(args), self.tool.USAGE, args)


class Tool(ApplyCase):

    def test_the_tool_runs_classify_apply(self):
        # The real machine lock and process scan: with no tree besides the main checkout there is
        # nothing to remove whatever they say.
        done = subprocess.run([sys.executable, TOOL, '-C', self.repo, '--classify', '--apply'],
                              cwd=self.repo, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', env=gitfixture.fixture_env(), timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('applied   removed 0, failed 0, skipped 0', done.stdout)

if __name__ == '__main__':
    unittest.main()
