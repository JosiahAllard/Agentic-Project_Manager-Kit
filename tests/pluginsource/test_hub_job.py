"""kitlib.hubsite.plugin_job: the hub's plugin refresh job (docs/plugin-source.md "Hub refresh").
A temp kit with a bare remote (tests.pluginsource Kit), a temp export root and a fake claude: the
job reads the kit's tracking ref and state.json, and runs kitlib.pluginsource.refresh itself."""
import os
import threading
import unittest

from kitlib import pluginsource as P
from kitlib.hubsite import jobs, plugin_job
from kitlib.pluginsource import claude
from tests.pluginsource import FakeProc, Kit

WAIT_S = 10


def ctx(stop=False):
    ev = threading.Event()
    if stop:
        ev.set()
    return jobs.Ctx(ev, lambda word: None)


class JobCase(unittest.TestCase):
    def setUp(self):
        self.k = Kit(self)
        self.auto = True
        self.folders = self.k.folders('.claude')
        self.proc = FakeProc()
        self.t = [1000.0]

    def settings(self, kit):
        return P.Settings(self.k.root, self.auto, 'origin')

    def job(self, **kw):
        kw.setdefault('kit', self.k.kit)
        kw.setdefault('load', self.settings)
        kw.setdefault('folders', lambda: self.folders)
        kw.setdefault('runner', claude.Runner(self.k.home, claude='claude.cmd', run_proc=self.proc))
        kw.setdefault('clock', lambda: self.t[0])
        kw.setdefault('in_use', None)       # no process read: the prune keeps its 7-day rule
        return plugin_job.PluginJob(**kw)

    def main(self):
        return self.k.repo.git('rev-parse', 'refs/remotes/origin/main')

    def counted(self, job):
        """Wrap job.refresh so each call is counted in self.refreshes."""
        self.refreshes = []
        real = job.refresh

        def refresh(kit, s, folders):
            self.refreshes.append(kit)
            return real(kit, s, folders)
        job.refresh = refresh
        return job


class PluginJobTest(JobCase):
    def test_behind_refreshes_once_then_current_does_nothing(self):
        self.k.seed_migrated(self.folders)
        job = self.counted(self.job())
        self.assertEqual(job(ctx()), 'refreshed to %s' % self.main()[:12])
        self.assertEqual(len(self.refreshes), 1)
        self.assertEqual([c[0] for c in self.proc.calls],
                         [['plugin', 'marketplace', 'update', 'pm-kit'],
                          ['plugin', 'update', 'pm-kit@pm-kit']])
        state = P.read_state(self.k.root)
        self.assertEqual(state['sha'], self.main())
        with open(os.path.join(self.k.root, P.LOGFILE), encoding='utf-8') as fh:
            log = fh.read()
        self.assertIn('hub plugin job ', log)
        self.assertIn('kit origin/main: %s' % self.main()[:12], log)
        self.assertEqual(job(ctx()), 'current at %s' % self.main()[:12])
        self.assertEqual(len(self.refreshes), 1, 'current: no second refresh')
        self.assertEqual(len(self.proc.calls), 2)

    def test_a_refresh_held_by_another_process_is_not_started(self):
        self.k.seed_migrated(self.folders)
        held = P.RefreshLock(self.k.root)
        held.acquire()
        self.addCleanup(held.release)
        job = self.counted(self.job())
        note = job(ctx())
        self.assertIn(plugin_job.BUSY, note)
        self.assertEqual(self.refreshes, [], 'refresh.pid is live: nothing is started')
        self.assertEqual(self.proc.calls, [])

    def test_a_refresh_that_finds_the_lock_taken_is_a_note_not_a_failure(self):
        self.k.seed_migrated(self.folders)
        job = self.job(refresh=lambda kit, s, folders: (1, ['REFUSED: %s (pid 9)' % plugin_job.BUSY]))
        self.assertIn(plugin_job.BUSY, job(ctx()))

    def test_two_runs_at_once_run_one_refresh(self):
        self.k.seed_migrated(self.folders)
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow(kit, s, folders):
            calls.append(kit)
            entered.set()
            release.wait(WAIT_S)
            return 0, ['done']
        js = jobs.Jobs([jobs.Job(plugin_job.NAME, 300, self.job(refresh=slow))])
        first = threading.Thread(target=js.run_now, args=(plugin_job.NAME,))
        first.start()
        self.assertTrue(entered.wait(WAIT_S))
        self.assertIs(js.run_now(plugin_job.NAME), False, 'the second run is refused')
        release.set()
        first.join(WAIT_S)
        self.assertEqual(len(calls), 1)

    def test_auto_refresh_off_reports_and_refreshes_nothing(self):
        self.auto = False
        self.k.seed_migrated(self.folders)
        job = self.counted(self.job())
        note = job(ctx())
        self.assertIn('behind kit main %s' % self.main()[:12], note)
        self.assertIn('plugin.auto_refresh is off', note)
        self.assertEqual(self.refreshes, [])

    def test_no_export_or_a_folder_not_migrated_refreshes_nothing(self):
        job = self.counted(self.job())
        self.assertIn('no export yet', job(ctx()))
        os.makedirs(self.k.root, exist_ok=True)
        P.write_state(self.k.root, {'folders': {}})
        self.assertIn('not migrated in %s' % self.folders[0], job(ctx()))
        self.assertEqual(self.refreshes, [])

    def test_a_folder_whose_last_refresh_failed_is_refreshed_again(self):
        self.k.seed_migrated(self.folders)
        self.job()(ctx())
        state = P.read_state(self.k.root)
        state['folders'][self.folders[0]]['rc'] = 1
        P.write_state(self.k.root, state)
        job = self.counted(self.job())
        self.assertEqual(job(ctx()), 'refreshed to %s' % self.main()[:12])
        self.assertEqual(len(self.refreshes), 1)

    def test_a_failed_refresh_raises_and_waits_retry_s_before_the_same_main_again(self):
        self.k.seed_migrated(self.folders)
        self.proc = FakeProc(fail={('<default>', 'marketplace'): 1})
        job = self.counted(self.job())
        with self.assertRaises(plugin_job.RefreshFailed) as got:
            job(ctx())
        self.assertIn('exited 1', str(got.exception))
        self.assertIn('FAILED', str(got.exception))
        self.t[0] += 60
        with self.assertRaises(plugin_job.RefreshFailed):
            job(ctx())
        self.assertEqual(len(self.refreshes), 1, 'not tried again before RETRY_S')
        self.t[0] += plugin_job.RETRY_S
        with self.assertRaises(plugin_job.RefreshFailed):
            job(ctx())
        self.assertEqual(len(self.refreshes), 2)

    def test_the_runner_counts_failures_of_the_job(self):
        self.k.seed_migrated(self.folders)
        self.proc = FakeProc(fail={('<default>', 'marketplace'): 1})
        events = []
        js = jobs.Jobs([jobs.Job(plugin_job.NAME, 300, self.job())],
                       notify=lambda n, w: events.append((n, w)))
        for _ in range(jobs.FAIL_PUSH):
            js.run_now(plugin_job.NAME)
        st = js.status()[plugin_job.NAME]
        self.assertEqual(st['failures'], jobs.FAIL_PUSH)
        self.assertIn('RefreshFailed', st['error'])
        self.assertEqual(events, [(plugin_job.NAME, 'failing')])

    def test_no_tracking_ref_is_a_note(self):
        self.k.repo.git('update-ref', '-d', 'refs/remotes/origin/main')
        self.assertIn('tracking ref', self.job()(ctx()))

    def test_the_host_copy_steps_run_and_one_that_raises_is_a_warning(self):
        self.k.seed_migrated(self.folders)
        ran = []

        def step(folder, out, apply):
            ran.append((os.path.basename(folder), apply))
            out('copy: done')
        job = self.job(copies=lambda: [step])
        job(ctx())
        self.assertEqual(ran, [(self.main()[:12], True)])

        def broken():
            raise RuntimeError('no host')
        lines = []
        job2 = self.job(copies=broken)
        self.assertEqual(job2._steps(lines.append), [])
        self.assertIn('WARNING: no host plugin copy is refreshed', lines[0])

    def test_a_stop_before_the_refresh_starts_none(self):
        self.k.seed_migrated(self.folders)
        job = self.counted(self.job())
        job(ctx(stop=True))
        self.assertEqual(self.refreshes, [])


if __name__ == '__main__':
    unittest.main()
