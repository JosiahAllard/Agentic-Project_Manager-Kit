"""kitlib.hubjobs.store_commits: the hub's hourly commit of the PM data worktree's backlog store,
on a temporary repository with a PM data record and its branch checked out in a linked worktree."""
import os
import threading
import time
import unittest
from unittest import mock

from kitlib import pmdata
from kitlib.backlog import errors
from kitlib.backlog import store as bstore
from kitlib.hubjobs import store_commits as sc
from kitlib.hubsite import jobs

from tests.backlog.helpers import priced
from tests.gitfixture import TempRepo
from tests.home import temp_home

DIR = 'kit/backlog'
KIT_JSON = '{"backlog": {"store": "kit/backlog"}}\n'


class StoreCommitsCase(unittest.TestCase):
    def setUp(self):
        temp_home(self)
        self.repo = TempRepo(bare_remote=True)
        self.addCleanup(self.repo.cleanup)
        pmdata.write(self.repo.common_dir(), 'pmdata', 'origin', self.repo.head())
        self.wt = self.repo.worktree('pm', branch='pmdata')
        self.repo.write('.kit.json', KIT_JSON, root=self.wt)
        for stem, text in list(priced('BAR').items()) + list(priced('BAZ').items()):
            self.repo.write('%s/%s.md' % (DIR, stem), text, root=self.wt)
        self.seed = self.repo.commit('seed the store', '.kit.json', DIR, cwd=self.wt)
        self.events = []
        self.job = sc.StoreCommits(root=self.wt, clock=lambda: time.time() + 3600)

    def run_job(self, job=None):
        ctx = jobs.Ctx(threading.Event(), lambda word, detail=None: self.events.append((word, detail)))
        return (job or self.job)(ctx)

    def edit(self, stem, text=None, rel=None):
        """Change a record file in the PM data worktree, uncommitted: by default BAR re-priced."""
        text = text if text is not None else priced(stem, priority='4')[stem]
        return self.repo.write(rel or '%s/%s.md' % (DIR, stem), text, root=self.wt)

    def head(self):
        return self.repo.git('rev-parse', 'HEAD', cwd=self.wt)

    def committed(self, rev='HEAD'):
        return self.repo.git('diff-tree', '--no-commit-id', '--name-only', '-r', rev,
                             cwd=self.wt).split()

    def status(self):
        return self.repo.git('status', '--porcelain', '--untracked-files=all', cwd=self.wt)


class Run(StoreCommitsCase):
    def test_nothing_pending_is_a_skip(self):
        out = self.run_job()
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('nothing to commit', out.note)
        self.assertEqual(self.head(), self.seed)
        self.assertEqual(self.events, [])

    def test_commits_only_the_uncommitted_record_paths(self):
        self.edit('BAR')
        self.repo.write('notes.txt', 'not the store\n', root=self.wt)
        self.repo.write('.kit.json', KIT_JSON + '\n', root=self.wt)
        out = self.run_job()
        self.assertIsInstance(out, str)
        self.assertIn('committed 1 record path', out)
        self.assertNotEqual(self.head(), self.seed)
        self.assertEqual(self.committed(), ['%s/BAR.md' % DIR])
        self.assertTrue(self.repo.git('log', '-1', '--format=%s', cwd=self.wt)
                        .startswith('backlog: hub commits 1 uncommitted record path'))
        self.assertEqual(self.events, [('committed', self.head()[:12])])
        st = self.status()
        self.assertNotIn('BAR.md', st)
        self.assertIn('?? notes.txt', st)
        # the other change stays as it was, unstaged; nothing is staged
        self.assertEqual(self.repo.git('diff', '--name-only', cwd=self.wt), '.kit.json')
        self.assertEqual(self.repo.git('diff', '--cached', '--name-only', cwd=self.wt), '')

    def test_a_deleted_record_is_committed_as_a_deletion(self):
        os.remove(os.path.join(self.wt, DIR, 'BAZ.md'))
        self.run_job()
        self.assertEqual(self.committed(), ['%s/BAZ.md' % DIR])
        self.assertEqual(self.status(), '')

    def test_never_pushes(self):
        before = self.repo.git('ls-remote', self.repo.remote)
        self.edit('BAR')
        self.run_job()
        self.assertNotEqual(self.head(), self.seed)
        self.assertEqual(self.repo.git('ls-remote', self.repo.remote), before)
        self.assertNotIn('pmdata', before)

    def test_a_held_index_lock_is_a_skip_and_commits_nothing(self):
        self.edit('BAR')
        index = self.repo.git('rev-parse', '--path-format=absolute', '--git-path', 'index',
                              cwd=self.wt)
        lock = os.path.normpath(index) + '.lock'
        open(lock, 'w').close()
        self.addCleanup(lambda: os.path.exists(lock) and os.remove(lock))
        with mock.patch.object(bstore, 'INDEX_WAIT', 0.1):
            out = self.run_job()
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('index lock', out.note)
        self.assertEqual(self.head(), self.seed)
        self.assertTrue(os.path.exists(lock))       # another git's lock is never removed
        os.remove(lock)
        self.assertIn('committed', self.run_job())  # the next pass commits it

    def test_a_record_changed_just_now_waits_for_a_later_pass(self):
        self.edit('BAR')
        job = sc.StoreCommits(root=self.wt)         # the real clock: the file is seconds old
        out = self.run_job(job)
        self.assertIn('left for a later pass', out)
        self.assertEqual(self.head(), self.seed)
        self.assertEqual(self.events, [])

    def test_a_half_written_record_is_left_out_and_fails(self):
        self.edit('BAR')
        self.edit('BAZ', text='id: BAZ\nsumm')      # cut off mid-header
        with self.assertRaises(sc.UnreadableRecord) as cm:
            self.run_job()
        self.assertIn('%s/BAZ.md' % DIR, str(cm.exception))
        self.assertIn('committed 1 record path', str(cm.exception))
        self.assertEqual(self.committed(), ['%s/BAR.md' % DIR])
        self.assertIn('BAZ.md', self.status())

    def test_an_empty_record_file_is_left_out(self):
        self.edit('BAZ', text='')
        with self.assertRaises(sc.UnreadableRecord):
            self.run_job()
        self.assertEqual(self.head(), self.seed)

    def test_a_record_whose_id_is_not_its_name_is_left_out(self):
        self.edit('BAZ', text=priced('QUX')['QUX'])
        with self.assertRaises(sc.UnreadableRecord) as cm:
            self.run_job()
        self.assertIn('not its file name', str(cm.exception))
        self.assertEqual(self.head(), self.seed)

    def test_a_path_that_is_not_a_record_is_never_committed(self):
        self.edit('x', text='scratch\n', rel='%s/notes.txt' % DIR)
        out = self.run_job()
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('not a record', out.note)
        self.assertEqual(self.head(), self.seed)

    def test_not_the_pm_data_worktree_is_a_skip(self):
        other = self.repo.worktree('elsewhere')
        self.repo.write('.kit.json', KIT_JSON, root=other)
        out = self.run_job(sc.StoreCommits(root=other))
        self.assertIsInstance(out, jobs.Skip)
        self.assertIn('not a folder store in the PM data worktree', out.note)


class Stops(StoreCommitsCase):
    def test_a_failed_commit_stops_the_job_until_the_paths_clear(self):
        self.edit('BAR')
        fail = errors.Uncommitted('git update-ref HEAD: boom', paths=['%s/BAR.md' % DIR])
        with mock.patch.object(bstore.MainBackend, 'commit_paths', side_effect=fail):
            with self.assertRaises(errors.Uncommitted):
                self.run_job()
        self.edit('BAZ')                            # a second path: still nothing is committed
        with mock.patch.object(bstore.MainBackend, 'commit_paths') as commit:
            with self.assertRaises(sc.StoreStuck) as cm:
                self.run_job()
            commit.assert_not_called()
        self.assertIn('boom', str(cm.exception))
        self.assertEqual(self.head(), self.seed)
        self.repo.commit('by hand', '%s/BAR.md' % DIR, cwd=self.wt)
        self.assertIn('committed 1 record path', self.run_job())
        self.assertEqual(self.committed(), ['%s/BAZ.md' % DIR])

    def test_a_stopped_job_counts_failures_in_the_runner(self):
        self.edit('BAR')
        fail = errors.IndexStale('index not updated', paths=['%s/BAR.md' % DIR])
        runner = jobs.Jobs([jobs.Job(sc.NAME, sc.EVERY_S, self.job)])
        with mock.patch.object(bstore.MainBackend, 'commit_paths', side_effect=fail):
            runner.run_now(sc.NAME)
        runner.run_now(sc.NAME)
        block = runner.status()[sc.NAME]
        self.assertEqual(block['failures'], 2)
        self.assertIn('StoreStuck', block['error'])


class Make(unittest.TestCase):
    def test_no_pm_data_worktree_gives_no_job(self):
        with mock.patch.object(pmdata, 'machine_settings', return_value=None):
            self.assertIsNone(sc.make(mock.Mock(home=None)))

    def test_an_unreadable_pm_data_setting_gives_no_job(self):
        with mock.patch.object(pmdata, 'machine_settings', side_effect=pmdata.PmDataError('bad')):
            self.assertIsNone(sc.make(mock.Mock(home=None)))

    def test_the_job_runs_hourly_on_the_pm_data_worktree(self):
        with mock.patch.object(pmdata, 'machine_settings',
                               return_value={'path': 'Z:/pm', 'branch': 'pmdata', 'remote': 'o'}):
            job = sc.make(mock.Mock(home=None))
        self.assertEqual((job.name, job.every_s), ('store-commits', 3600))
        self.assertEqual(job.run.root, 'Z:/pm')


if __name__ == '__main__':
    unittest.main()
