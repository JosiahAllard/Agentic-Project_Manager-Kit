"""Machine setup's scheduled jobs on Linux (kitlib.machinesetup.linuxjobs, docs/linux-support-plan.md
slice 10): the agent temp sweep, the worktree sweep and the automatic tasks' tick planned as pm-kit
jobs, procwatch's note and linger's note. kitlib.platform.NAME is patched to 'linux'; the host is
a fake whose jobs go to a temporary home through the real schedule module with a fake systemctl,
so nothing real is written, and linger is only ever read."""
import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from kitlib.git import repo as gitrepo
from kitlib.machinesetup import agenttemp, extras, linuxjobs, procwatch, worktreesweep
from kitlib.machinesetup.common import Plan
from kitlib.platform.linux import schedule
from kitlib.worktree import task as wtask
from tests.platform.test_linux_schedule import FakeSystem, which_none


class JobHost(object):
    """The Linux half of extras.Host over a temporary home."""

    def __init__(self, home, systemd=True, linger=False):
        self.home, self.run = home, FakeSystem(systemd=systemd)
        self._linger, self.calls = linger, []

    def task_exists(self, name):
        return schedule.exists(name, self.home, self.run, which_none)

    def task_list(self):
        return schedule.read_back(self.home, self.run, which_none)

    def job_backend(self, job):
        return schedule.choose(job, self.run, which_none, {})

    def job_describe(self, job, kind):
        return schedule.describe(job, kind, self.home)

    def job_install(self, job):
        self.calls.append(('install', job.name))
        return schedule.install(job, None, self.home, self.run, which_none, {})

    def systemd(self):
        return self.run.systemd

    def linger(self):
        return self._linger

    def sweep_dry_run(self, kit, home=None):
        self.calls.append(('sweep-dry-run', kit))
        return ['nothing to remove']

    def worktree_sweep_dry_run(self, tool, main):
        self.calls.append(('worktree-dry-run', main))
        return ['nothing to remove']

    def task_register(self, line):
        raise AssertionError('no PowerShell line on Linux')


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='linux-jobs-')
        self.addCleanup(shutil.rmtree, self.root, True)
        self.home = os.path.join(self.root, 'home')
        self.kit = os.path.join(self.root, 'kit')
        os.makedirs(os.path.join(self.kit, 'tools'))
        self.host = JobHost(self.home)
        patch = mock.patch('kitlib.platform.NAME', 'linux')
        patch.start()
        self.addCleanup(patch.stop)
        self.py = os.path.abspath(sys.executable)
        patch = mock.patch.object(schedule, 'python', lambda: self.py)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, **kw):
        kw.setdefault('sweep_task', True)
        return SimpleNamespace(host=self.host, kit=self.kit, home=self.home, **kw)

    def apply(self, plan):
        for c in plan.changes:
            c.do()

    def text(self, plan):
        return '\n'.join(plan.lines())


class AgentTemp(Base):
    def test_a_missing_job_is_the_dry_run_then_the_daily_job(self):
        plan = Plan()
        agenttemp.task_step(plan, self.ctx())
        self.assertEqual(len(plan.changes), 2, self.text(plan))
        self.assertIn('run the sweep once without --apply', plan.changes[0].text)
        self.assertIn('write the pm-kit job agent-temp-sweep (daily at 04:00, as you, systemd)',
                      plan.changes[1].text)
        self.assertIn('systemctl --user disable --now pm-kit-agent-temp-sweep.timer', plan.changes[1].undo)
        self.apply(plan)
        self.assertEqual(self.host.calls, [('sweep-dry-run', self.kit), ('install', 'agent-temp-sweep')])
        task = schedule.find(agenttemp.TASK, self.host.task_list())
        self.assertTrue(schedule.same(task, agenttemp.linux_job(self.kit)), task)
        self.assertEqual(task['argv'][1:], [os.path.join(self.kit, agenttemp.SWEEP), '--apply'])

    def test_a_written_job_is_left_and_task_registered_reads_it_back(self):
        plan = Plan()
        agenttemp.task_step(plan, self.ctx())
        self.apply(plan)
        plan = Plan()
        agenttemp.task_step(plan, self.ctx())
        self.assertEqual(plan.changes, [])
        with mock.patch.object(schedule, 'exists', return_value=True) as got:
            self.assertTrue(agenttemp.task_registered(agenttemp.TASK, run='RUN'))
        got.assert_called_once_with(agenttemp.TASK, run='RUN')

    def test_no_sweep_task_is_a_note(self):
        plan = Plan()
        agenttemp.task_step(plan, self.ctx(sweep_task=False))
        self.assertEqual(plan.changes, [])
        self.assertIn('pm-kit job agent-temp-sweep is not written (--no-sweep-task)', self.text(plan))

    def test_no_backend_is_a_note_not_a_change(self):
        self.host.run.systemd = False
        plan = Plan()
        agenttemp.task_step(plan, self.ctx())
        self.assertEqual(len(plan.changes), 1)          # the dry run only
        self.assertIn('pm-kit job agent-temp-sweep not planned: job agent-temp-sweep: no user systemd',
                      self.text(plan))


class WorktreeSweep(Base):
    def setUp(self):
        super().setUp()
        self.main = os.path.join(self.root, 'repo')
        self.tool = os.path.join(self.kit, 'tools', 'worktree_remove.py')

    def step(self, plan, **kw):
        worktreesweep._repo_step(plan, self.ctx(**kw), 'demo', self.main, 'demo', self.tool,
                                 self.host.task_list(), None)

    def test_a_missing_job_is_the_dry_run_then_the_job_and_then_nothing(self):
        plan = Plan()
        self.step(plan)
        self.assertEqual(len(plan.changes), 2, self.text(plan))
        self.assertIn('write the pm-kit job demo-worktree-sweep (daily at 03:00', plan.changes[1].text)
        self.apply(plan)
        task = schedule.find('demo-worktree-sweep', self.host.task_list())
        self.assertEqual(task['argv'], [self.py, self.tool, '-C', self.main, '--sweep-merged', '--apply'])
        self.assertTrue(schedule.same(task, wtask.linux_job(self.main, 'demo', self.tool, self.py)))
        plan = Plan()
        self.step(plan)
        self.assertEqual(plan.changes, [])

    def test_a_job_that_differs_is_updated(self):
        plan = Plan()
        self.step(plan)
        self.apply(plan)
        self.tool = os.path.join(self.root, 'other-kit', 'tools', 'worktree_remove.py')
        plan = Plan()
        self.step(plan)
        self.assertIn('update the pm-kit job demo-worktree-sweep', plan.changes[1].text)

    def test_no_worktree_sweep_is_a_note(self):
        plan = Plan()
        self.step(plan, worktree_sweep=False)
        self.assertEqual(plan.changes, [])
        self.assertIn('pm-kit job demo-worktree-sweep (demo) is not written (--no-worktree-sweep)',
                      self.text(plan))


class AutoTask(Base):
    def setUp(self):
        super().setUp()
        with open(os.path.join(self.kit, extras.AUTO_TOOL), 'w') as f:
            f.write('')
        place = gitrepo.Place(gitrepo.MAIN, self.kit, '', None, None)
        for target, value in (('kitlib.machinesetup.worktreesweep.tool_root', lambda kit, environ=None: self.kit),
                              ('kitlib.git.repo.classify', lambda path, *a, **k: place)):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_the_tick_is_every_15_minutes_ended_after_10_and_written_once(self):
        plan = Plan()
        extras.auto_task_step(plan, self.ctx())
        self.assertEqual(len(plan.changes), 1, self.text(plan))
        self.assertIn('write the pm-kit job kit-automatic-tasks (every 15 min', plan.changes[0].text)
        self.apply(plan)
        task = schedule.find(extras.AUTO_TASK, self.host.task_list())
        self.assertEqual(task['argv'], [self.py, os.path.join(self.kit, extras.AUTO_TOOL), 'tick'])
        self.assertEqual((task['when'], task['limit_min']), (('every', 15), 10))
        plan = Plan()
        extras.auto_task_step(plan, self.ctx())
        self.assertEqual(plan.changes, [])

    def test_a_tick_that_differs_is_updated(self):
        old = extras.auto_job(self.kit)._replace(when=('every', 5))
        schedule.install(old, schedule.SYSTEMD, self.home, self.host.run)
        plan = Plan()
        extras.auto_task_step(plan, self.ctx())
        self.assertIn('update the pm-kit job kit-automatic-tasks', plan.changes[0].text)


class Notes(Base):
    def test_linger_off_with_systemd_is_a_note_with_the_command_and_nothing_runs_it(self):
        plan = Plan()
        linuxjobs.linger_note(plan, self.ctx())
        self.assertEqual(plan.changes, [])
        self.assertIn(schedule.LINGER_COMMAND, self.text(plan))
        self.assertFalse([a for a, _ in self.host.run.calls if a[0] == 'loginctl'])

    def test_no_note_when_linger_is_on_unknown_or_there_is_no_systemd(self):
        for systemd, linger in ((True, True), (True, None), (False, False)):
            self.host = JobHost(self.home, systemd, linger)
            plan = Plan()
            linuxjobs.linger_note(plan, self.ctx())
            self.assertNotIn('linger', self.text(plan), (systemd, linger))

    def test_procwatch_is_a_note_on_linux(self):
        plan = Plan()
        procwatch.task_step(plan, self.ctx(procwatch=True), os.path.join(self.root, 'temp'))
        self.assertEqual(plan.changes, [])
        self.assertIn('procwatch task: not on Linux yet', self.text(plan))
        plan = Plan()
        procwatch.task_step(plan, self.ctx(procwatch=False), None)
        self.assertNotIn('procwatch', self.text(plan))


class Host(Base):
    def test_the_host_reads_and_writes_through_schedule(self):
        host = extras.Host()
        job = agenttemp.linux_job(self.kit)
        for method, args, target in (('task_exists', ('x',), 'exists'), ('job_backend', (job,), 'choose'),
                                     ('job_describe', (job, 'cron'), 'describe'),
                                     ('job_install', (job,), 'install'), ('systemd', (), 'has_systemd'),
                                     ('linger', (), 'linger')):
            with mock.patch.object(schedule, target, return_value=['ok']) as got:
                getattr(host, method)(*args)
            self.assertTrue(got.called, method)
        with mock.patch.object(schedule, 'read_back', return_value=[]) as got:
            self.assertEqual(host.task_list(), [])
        self.assertTrue(got.called)


if __name__ == '__main__':
    unittest.main()
