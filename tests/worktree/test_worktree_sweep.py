"""kitlib.worktree.sweep (`worktree_remove.py --sweep-merged [--apply]`): what it sweeps, what it
skips and why, and every run-wide check failing closed. Throwaway repositories only
(tests/gitfixture.py); the machine lock is a seam, never the real one.
"""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from kitlib import kitroot
from kitlib.config import user
from kitlib.git import defaults as gitdefaults
from kitlib.worktree import sweep
from tests import gitfixture
from tests.worktree.test_worktree_remove import RemoveCase

LATER = 3 * 24 * 3600


def free():
    return [], None


class SweepCase(RemoveCase):

    def setUp(self):
        super().setUp()
        self.logs = tempfile.mkdtemp(prefix='kit-sweep-log-')
        self.addCleanup(gitfixture.rmtree, self.logs)

    def sweep(self, apply=False, machine=free, now=None, top=None):
        got = sweep.sweep(top or self.repo, apply=apply, machine=machine,
                          now=time.time() + LATER if now is None else now, log_dir=self.logs)
        return got.code, '\n'.join(got.lines)

    def landed(self, slug):
        path = self.workstream(slug)
        self.land('kit/' + slug)
        return path


class Sweep(SweepCase):

    def test_a_dry_run_names_what_it_would_remove_and_removes_nothing(self):
        path = self.landed('done')
        rc, out = self.sweep()
        self.assertEqual(rc, 0, out)
        self.assertIn('sweep     dry run', out)
        self.assertIn('base      origin/main ', out)
        self.assertIn('would     remove %s, then git branch -d kit/done' % path, out)
        self.assertIn('summary   removed 0, would remove 1, skipped 1, failed 0', out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(os.listdir(self.logs), [])

    def test_apply_removes_and_logs(self):
        path = self.landed('done')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('removed   %s' % path, out)
        self.assertIn('deleted   branch kit/done', out)
        self.assertFalse(os.path.exists(path))
        log = os.path.join(self.logs, '%s-worktree-sweep.log' % gitdefaults.repo_name(self.repo))
        with open(log, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('removed   %s' % path, text)
        self.assertTrue(text.endswith('exit      0\n\n'), text)

    def test_the_sweep_fetches_the_base(self):
        path = self.workstream('remote')
        # Landed on the remote by someone else: only a fetch shows it.
        self.git('push', '-q', 'origin', 'kit/remote:refs/heads/side')
        other = os.path.join(self.fx.base, 'other')
        self.git('clone', '-q', '-b', 'main', self.fx.remote, other, cwd=self.fx.base)
        self.git('config', 'user.name', gitfixture.NAME, cwd=other)
        self.git('config', 'user.email', gitfixture.EMAIL, cwd=other)
        self.git('merge', '-q', '--no-ff', '-m', 'Merge side', 'origin/side', cwd=other)
        self.git('push', '-q', 'origin', 'main', cwd=other)
        rc, out = self.sweep()
        self.assertEqual(rc, 0, out)
        self.assertIn('would     remove %s' % path, out)

    def test_a_fresh_worktree_is_never_swept(self):
        path = self.workstream('fresh', commits=0)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('no commit of its own has landed', out)
        self.assertTrue(os.path.isdir(path))

    def test_unlanded_work_is_skipped(self):
        path = self.workstream('open')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('UNLANDED: kit/open has 1 commit(s)', out)
        self.assertTrue(os.path.isdir(path))

    def test_recent_activity_is_skipped(self):
        path = self.landed('busy')
        rc, out = self.sweep(apply=True, now=time.time())
        self.assertEqual(rc, 0, out)
        self.assertIn('under the 24 h idle bar', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_machine_lock_record_naming_it_is_skipped(self):
        path = self.landed('gpu')

        def machine():
            return [{'repo': 'r', 'slug': 'x', 'id': 7, 'note': 'training in .claude/worktrees/gpu'}], None
        rc, out = self.sweep(apply=True, machine=machine)
        self.assertEqual(rc, 0, out)
        self.assertIn('machine lock holder: r/x run 7', out)
        self.assertTrue(os.path.isdir(path))

    def test_an_unreadable_machine_lock_defers(self):
        path = self.landed('m')
        rc, out = self.sweep(apply=True, machine=lambda: (None, 'tool missing'))
        self.assertEqual(rc, 1, out)
        self.assertIn('deferred  the machine lock cannot be read (tool missing)', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_held_landing_lock_defers_before_anything(self):
        path = self.landed('l')
        with open(os.path.join(self.fx.common_dir(), 'kit-landing.lock'), 'w') as f:
            f.write('not a record')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('deferred  the landing lock', out)
        self.assertNotIn('base      ', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_landing_that_starts_mid_sweep_stops_it(self):
        path = self.landed('mid')
        lock = os.path.join(self.fx.common_dir(), 'kit-landing.lock')

        def machine():
            # Read after the run-wide landing check: the lock appears between it and the removal.
            with open(lock, 'w') as f:
                f.write('{}')
            return [], None
        rc, out = self.sweep(apply=True, machine=machine)
        self.assertEqual(rc, 1, out)
        self.assertIn('a landing started during the sweep', out)
        self.assertIn('stopped by the landing lock', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_failed_fetch_removes_nothing(self):
        path = self.landed('f')
        self.git('remote', 'set-url', 'origin', os.path.join(self.fx.base, 'missing.git'))
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    the fetch of origin/main failed', out)
        self.assertTrue(os.path.isdir(path))

    def test_detached_and_locked_are_never_swept(self):
        det = self.landed('det')
        self.git('checkout', '-q', '--detach', cwd=det)
        locked = self.landed('lk')
        self.git('worktree', 'lock', '--reason', 'kept', locked)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('detached HEAD: no branch, never swept', out)
        self.assertIn('locked with `git worktree lock`: kept', out)
        self.assertTrue(os.path.isdir(det) and os.path.isdir(locked))
        self.git('worktree', 'unlock', locked)

    def test_dirty_is_skipped(self):
        path = self.landed('d')
        self.fx.write('wip.txt', 'x\n', root=path)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('uncommitted or untracked files (1): ?? wip.txt', out)
        self.assertTrue(os.path.isdir(path))

    def test_an_ignored_file_written_after_the_sweeps_second_status_read_survives(self):
        self.fx.write('.gitignore', '*.log\n')
        self.fx.commit('ignore', '.gitignore')
        self.fx.git('push', '-q', 'origin', 'main')
        path = self.landed('late')
        late = os.path.join(path, 'run.log')
        real, seen = sweep.sweep_status, []

        def status(ctx, p):
            got = real(ctx, p)
            seen.append(p)
            if len(seen) == 2:      # the read just before git: what comes now, the sweep never saw
                self.fx.write('run.log', 'mine\n', root=path)
            return got

        with mock.patch.object(sweep, 'sweep_status', status):
            rc, out = self.sweep(apply=True)
        self.assertTrue(os.path.isfile(late), out)
        self.assertEqual([os.path.normcase(p) for p in seen], [os.path.normcase(path)] * 2, out)
        self.assertEqual(rc, 1, out)
        self.assertIn('ignored files that are not build output (1): !! run.log', out)
        self.assertIn('git was NOT run', out)
        self.assertNotIn('removed   %s' % path, out)
        self.assertIn('summary   removed 0, would remove 0, skipped 1, failed 1', out)
        self.assertIn(os.path.normcase(path), self.registered(), out)
        self.assertIn('kit/late', self.branches())

    def test_it_runs_on_the_main_checkout_only(self):
        path = self.landed('x')
        rc, out = self.sweep(top=path)
        self.assertEqual(rc, 1, out)
        self.assertIn('the sweep runs on the main checkout only', out)


class SweepOpenClaim(SweepCase):

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_pulled_record_on_the_base_is_an_open_claim(self):
        path = self.workstream('claim')
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: kit/claim\n')
        self.fx.commit('claim', 'docs/backlog/T-002.md')
        self.land('kit/claim')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('a backlog record is still pulled by kit/claim (an open claim): T-002', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_done_record_is_no_claim(self):
        path = self.workstream('done')
        self.fx.write('docs/backlog/T-003.md', 'id: T-003\nstate: done\npulled_by: kit/done\n')
        self.fx.commit('done', 'docs/backlog/T-003.md')
        self.land('kit/done')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path))


class MachineLockEntries(unittest.TestCase):
    """The lock tool is the kit's own tools/machine_lock.py (kitlib.kitroot.lock_tool), never one
    under the home folder: a temp home with no `.claude` in it stands in for the user's."""

    def setUp(self):
        self.kit = tempfile.mkdtemp(prefix='kit-')
        self.addCleanup(gitfixture.rmtree, self.kit)
        self.tool = sweep.machine_lock_path(self.kit)
        os.makedirs(os.path.dirname(self.tool))
        with open(self.tool, 'w') as f:
            f.write('# never run: the runner is faked\n')
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(gitfixture.rmtree, self.home)
        patch = mock.patch.object(user, 'home_dir', return_value=self.home)
        patch.start()
        self.addCleanup(patch.stop)

    def entries(self, stdout, rc=0):
        def run(args, **kw):
            self.assertEqual(args[-2:], ['status', '--json'])
            self.assertEqual(args[-3], self.tool)
            return subprocess.CompletedProcess(args, rc, stdout, '')
        return sweep.machine_lock_entries(run=run, kit=self.kit)

    def test_the_default_is_the_kits_own_lock_tool(self):
        self.assertEqual(sweep.machine_lock_path(), kitroot.lock_tool())
        self.assertEqual(sweep.machine_lock_path(self.kit), os.path.join(self.kit, 'tools', 'machine_lock.py'))
        ran = []

        def run(args, **kw):
            ran.append(args)
            return subprocess.CompletedProcess(args, 0, '{"runs": [], "queue": {"entries": []}}', '')
        self.assertEqual(sweep.machine_lock_entries(run=run), ([], None))
        self.assertEqual(ran[0][-3], kitroot.lock_tool())
        self.assertFalse(os.path.normcase(ran[0][-3]).startswith(os.path.normcase(self.home)), ran[0])

    def test_the_pm_shape(self):
        doc = {'lock': {'held': [{'id': 1}], 'waiting': [{'id': 2}], 'background': [{'id': 3}]}}
        self.assertEqual(self.entries(json.dumps(doc)), ([{'id': 1}, {'id': 2}, {'id': 3}], None))

    def test_the_kit_shape(self):
        doc = {'runs': [{'id': 1}], 'queue': {'entries': [{'id': 2}]}}
        self.assertEqual(self.entries(json.dumps(doc)), ([{'id': 1}, {'id': 2}], None))

    def test_anything_else_fails_closed(self):
        for text, rc in (('{}', 0), ('not json', 0), ('{"lock": {"held": []}}', 3),
                         ('{"lock": {"held": [], "waiting": 5}}', 0)):
            got, why = self.entries(text, rc)
            self.assertIsNone(got, text)
            self.assertTrue(why)

    def test_a_missing_tool_fails_closed(self):
        run = mock.Mock()
        got, why = sweep.machine_lock_entries(run=run, kit=os.path.join(self.kit, 'no'))
        self.assertIsNone(got)
        self.assertIn('not at', why)
        run.assert_not_called()

    def test_holder_matching(self):
        path = os.path.join('C:' + os.sep, 'r', '.claude', 'worktrees', 'slug')
        self.assertIsNone(sweep.machine_lock_holder([{'note': 'other'}], path, 'kit/slug'))
        self.assertTrue(sweep.machine_lock_holder([{'note': 'on KIT/SLUG'}], path, 'kit/slug'))
        self.assertTrue(sweep.machine_lock_holder([{'cwd': 'c:/r/.claude/worktrees/slug'}],
                                                  path, 'kit/x'))


if __name__ == '__main__':
    unittest.main()
