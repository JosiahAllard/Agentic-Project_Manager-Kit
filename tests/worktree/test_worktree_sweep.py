"""kitlib.worktree.sweep (`worktree_remove.py --sweep-merged [--apply]`): what it sweeps, what it
skips and why, and every run-wide check failing closed. Throwaway repositories only
(tests/gitfixture.py); the machine lock is a seam, never the real one.
"""
import datetime
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


MARKS_FILE = 'kit-stale-marks.json'      # kitlib.worktree.marks.FILE_NAME, spelled out: see StaleMarkCase


def stamp(when):
    return datetime.datetime.fromtimestamp(when, datetime.timezone.utc).strftime(
        '%Y-%m-%dT%H:%M:%S.%fZ')


class StaleMarkCase(SweepCase):
    """The marks file is written here as JSON, not through kitlib.worktree.marks, so the invariant
    test runs (and fails on its assertions) against a kit without stale marks: red-first."""

    def marks_path(self):
        return os.path.join(self.fx.common_dir(), MARKS_FILE)

    def record(self, slug, path, why='superseded by kit/other', at=None):
        return {'branch': 'kit/' + slug, 'tip': self.git('rev-parse', 'kit/' + slug),
                'path': path, 'why': why, 'at': stamp(time.time() if at is None else at),
                'by': 'coord-test (coordinator)'}

    def write_marks(self, *records, text=None):
        with open(self.marks_path(), 'w', encoding='utf-8', newline='\n') as f:
            f.write(text if text is not None else json.dumps(
                {'version': 1, 'marks': list(records)}, indent=2) + '\n')

    def read_marks(self):
        with open(self.marks_path(), encoding='utf-8') as f:
            return json.load(f)['marks']

    def tag_tip(self, tag):
        return self.git('rev-parse', '-q', '--verify', 'refs/tags/%s^{commit}' % tag, check=False)


class StaleMarks(StaleMarkCase):

    def test_only_the_marked_unlanded_tree_is_abandoned(self):
        """The invariant: an UNLANDED tree is abandoned only when a stale mark names it; its
        unmarked neighbour, as idle and as unlanded, stays as it is."""
        gone = self.workstream('gone', commits=2)
        kept = self.workstream('kept')
        tip, kept_tip = self.git('rev-parse', 'kit/gone'), self.git('rev-parse', 'kit/kept')
        self.write_marks(self.record('gone', gone))
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(gone), out)
        self.assertNotIn(os.path.normcase(gone), self.registered(), out)
        self.assertNotIn('kit/gone', self.branches(), out)
        self.assertEqual(self.tag_tip('archive/gone'), tip, out)
        message = self.git('cat-file', 'tag', 'archive/gone')
        self.assertIn('stale (marked ', message)
        self.assertIn('by coord-test (coordinator)): superseded by kit/other', message)
        self.assertEqual(self.read_marks(), [], out)
        self.assertIn('UNLANDED: kit/kept has 1 commit(s)', out)
        self.assertTrue(os.path.isdir(kept), out)
        self.assertIn('kit/kept', self.branches(), out)
        self.assertEqual(self.git('rev-parse', 'kit/kept'), kept_tip)
        self.assertEqual(self.tag_tip('archive/kept'), '', out)

    def test_a_dry_run_names_the_abandon_and_changes_nothing(self):
        path = self.workstream('gone')
        mark = self.record('gone', path)
        self.write_marks(mark)
        rc, out = self.sweep()
        self.assertEqual(rc, 0, out)
        self.assertIn('would     abandon %s under archive/gone (marked stale ' % path, out)
        self.assertIn('would abandon 1', out)
        self.assertTrue(os.path.isdir(path))
        self.assertIn('kit/gone', self.branches())
        self.assertEqual(self.tag_tip('archive/gone'), '')
        self.assertEqual(self.read_marks(), [mark])

    def test_a_moved_tip_voids_the_mark(self):
        path = self.workstream('gone')
        mark = self.record('gone', path, at=time.time() + 60)
        self.write_marks(mark)
        self.fx.write('more.txt', 'more\n', root=path)
        self.fx.commit('more', 'more.txt', cwd=path)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('UNLANDED: kit/gone has 2 commit(s)', out)
        self.assertIn('its stale mark is void: the tip moved from %s' % mark['tip'][:10], out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.read_marks(), [mark])

    def test_activity_after_the_mark_voids_it(self):
        path = self.workstream('gone')
        mark = self.record('gone', path)
        self.write_marks(mark)
        later = time.time() + 120
        os.utime(os.path.join(path, 'gone-0.txt'), (later, later))
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('its stale mark is void: ', out)
        self.assertIn('gone-0.txt changed at ', out)
        self.assertTrue(os.path.isdir(path))
        self.assertIn('kit/gone', self.branches())
        self.assertEqual(self.read_marks(), [mark])

    def test_a_mark_naming_another_tree_is_void(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', os.path.join(self.home, 'elsewhere')))
        rc, out = self.sweep(apply=True)
        self.assertIn('its stale mark is void: it names the worktree', out)
        self.assertTrue(os.path.isdir(path))

    def test_under_the_idle_bar_it_waits(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))
        rc, out = self.sweep(apply=True, now=time.time())
        self.assertEqual(rc, 0, out)
        self.assertIn('under the 24 h idle bar', out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(len(self.read_marks()), 1)

    def test_a_machine_lock_record_keeps_it(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))

        def machine():
            return [{'repo': 'r', 'slug': 'x', 'id': 7, 'note': 'run in .claude/worktrees/gone'}], None
        rc, out = self.sweep(apply=True, machine=machine)
        self.assertEqual(rc, 0, out)
        self.assertIn('machine lock holder: r/x run 7', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_git_worktree_lock_keeps_it(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))
        self.git('worktree', 'lock', '--reason', 'kept', path)
        self.addCleanup(self.git, 'worktree', 'unlock', path, check=False)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('locked with `git worktree lock`: kept', out)
        self.assertTrue(os.path.isdir(path))

    def test_dirt_keeps_it(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path, at=time.time() + 60))
        self.fx.write('wip.txt', 'x\n', root=path)
        os.utime(os.path.join(path, 'wip.txt'), (time.time() - 60, time.time() - 60))
        rc, out = self.sweep(apply=True)
        self.assertIn('uncommitted or untracked files (1): ?? wip.txt', out)
        self.assertTrue(os.path.isdir(path))

    def test_a_landing_that_starts_mid_sweep_stops_the_abandon(self):
        path = self.workstream('gone')
        mark = self.record('gone', path)
        self.write_marks(mark)
        lock = os.path.join(self.fx.common_dir(), 'kit-landing.lock')

        def machine():
            with open(lock, 'w') as f:
                f.write('{}')
            return [], None
        rc, out = self.sweep(apply=True, machine=machine)
        self.assertEqual(rc, 1, out)
        self.assertIn('a landing started during the sweep', out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.tag_tip('archive/gone'), '')
        self.assertEqual(self.read_marks(), [mark])

    def test_an_unreadable_marks_file_abandons_nothing_and_the_landed_sweep_goes_on(self):
        gone = self.workstream('gone')
        done = self.landed('done')
        self.write_marks(text='{"version": 1, "marks": [{"branch": "kit/gone"}]}\n')
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('failed    the stale marks cannot be read (', out)
        self.assertIn('no marked worktree is abandoned; the landed sweep goes on', out)
        self.assertIn('UNLANDED: kit/gone', out)
        self.assertTrue(os.path.isdir(gone))
        self.assertFalse(os.path.exists(done), out)
        self.assertIn('removed 1', out)

    def test_a_mark_changed_after_it_was_read_is_skipped(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))
        real, seen = sweep.sweep_status, []

        def status(ctx, p):
            seen.append(p)
            if len(seen) == 2:      # the read just before the abandon: the mark is re-written now
                self.write_marks(self.record('gone', path, why='another reason'))
            return real(ctx, p)
        with mock.patch.object(sweep, 'sweep_status', status):
            rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('its stale mark changed or was cleared since it was read', out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.tag_tip('archive/gone'), '')
        self.assertEqual(self.read_marks()[0]['why'], 'another reason')

    def test_a_gone_branchs_mark_is_dropped_under_apply_only(self):
        path = self.workstream('gone')
        mark = self.record('gone', path)
        self.write_marks(mark)
        self.git('worktree', 'remove', '--force', path)
        self.git('branch', '-D', 'kit/gone')
        rc, out = self.sweep()
        self.assertEqual(rc, 0, out)
        self.assertIn('would     drop the stale mark of kit/gone: the branch is gone', out)
        self.assertEqual(self.read_marks(), [mark])
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('dropped   the stale mark of kit/gone: the branch is gone', out)
        self.assertEqual(self.read_marks(), [])

    def test_a_mark_on_a_branch_with_no_worktree_is_named_for_unmark(self):
        path = self.workstream('gone')
        mark = self.record('gone', path)
        self.write_marks(mark)
        self.git('worktree', 'remove', '--force', path)
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('note      the stale mark of kit/gone names no worktree', out)
        self.assertIn('kit/gone --unmark-stale', out)
        self.assertIn('kit/gone', self.branches())
        self.assertEqual(self.read_marks(), [mark])

    def test_a_mark_made_from_another_session_and_temp_folder_is_read(self):
        from kitlib.worktree import marks
        path = self.workstream('gone')
        other = tempfile.mkdtemp(prefix='kit-other-temp-')
        self.addCleanup(gitfixture.rmtree, other)
        with mock.patch.dict(os.environ, {'AGENT_TEMP': other, 'PM_KIT_SESSION_NAME': 'coord-a',
                                          'PM_KIT_ROLE': 'coordinator'}):
            got = marks.mark(path, 'kit/gone', 'superseded', liveness=lambda m, b: (None, 'tip'))
        self.assertEqual(got.code, 0, got.lines)
        with mock.patch.dict(os.environ, {'AGENT_TEMP': self.logs, 'PM_KIT_SESSION_NAME': 'sweep'}):
            # Activity after the mark is judged against the mark's real time: none here.
            rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(path), out)
        self.assertIn('by coord-a (coordinator)', self.git('cat-file', 'tag',
                                                           'archive/gone'))

    def test_the_abandon_is_held_to_the_checked_tip(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))
        real, seen = sweep.sweep_status, []

        def status(ctx, p):
            seen.append(p)
            if len(seen) == 2:      # a commit lands on the branch between the checks and the abandon
                self.fx.write('late.txt', 'late\n', root=path)
                self.fx.commit('late', 'late.txt', cwd=path)
            return real(ctx, p)
        with mock.patch.object(sweep, 'sweep_status', status):
            rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('not the %s it was checked at' % self.read_marks()[0]['tip'][:10], out)
        self.assertIn('the stale mark of kit/gone stays', out)
        self.assertTrue(os.path.isdir(path))
        self.assertEqual(self.tag_tip('archive/gone'), '')


class StaleMarkExempt(StaleMarkCase):

    CONFIG = {'worktree_remove': {'exempt': {'gone': 'kept by ruling X'}}}

    def test_an_exempt_tree_is_never_abandoned(self):
        path = self.workstream('gone')
        self.write_marks(self.record('gone', path))
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('exempt (kept by ruling X)', out)
        self.assertTrue(os.path.isdir(path))


class StaleMarkOpenClaim(StaleMarkCase):

    CONFIG = {'backlog': {'store': 'main'}}

    def test_a_claim_in_another_case_keeps_it(self):
        path = self.workstream('gone')
        self.fx.write('docs/backlog/T-002.md', 'id: T-002\nstate: pulled\npulled_by: KIT/Gone\n')
        self.fx.commit('claim', 'docs/backlog/T-002.md')
        self.fx.git('push', '-q', 'origin', 'main')
        self.write_marks(self.record('gone', path))
        rc, out = self.sweep(apply=True)
        self.assertEqual(rc, 0, out)
        self.assertIn('a backlog record is still pulled by kit/gone (an open claim): T-002', out)
        self.assertTrue(os.path.isdir(path))


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
