"""kitlib.machinesetup.worktreesweep (docs/machine-setup.md "Extras", the worktree sweep tasks) and
pm-start's check of those tasks (kitlib.pmstart.worktree_sweep_lines): the task line, the stale
export test, the tool's root, the step through machine setup against the fake host of
test_machinesetup, the --no-worktree-sweep flag, and the session-start lines. Nothing here reads or
writes the real task scheduler: the host's task list, dry run and registration are fakes, and the
pm-start check is given its readers.
"""
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib import pluginsource, pmstart
from kitlib.config import user as kuser
from kitlib.machinesetup import agenttemp, extras, worktreesweep as W
from kitlib.worktree import task as T
from tests import gitfixture
from tests.home import machine_config, no_machine_owner, temp_home
from tests.machinesetup import test_machinesetup as MS
from tests.pmstart import keep_off_launcher_repos

PYW = 'C:\\Py\\pythonw.exe'
GONE = 'bbbbbbbbbbbb'                 # an export folder with no marker: pruned
THERE = 'aaaaaaaaaaaa'                # an export folder that still holds its marker


def setUpModule():
    keep_off_launcher_repos()


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f)


def sweep_task(name, kit, main):
    """A registered task as consoletasks.parse gives it, running `kit`'s sweep for `main`."""
    return {'path': '\\' + T.task_name(name),
            'actions': [{'exe': PYW, 'args': T.argument(os.path.join(kit, 'tools', 'worktree_remove.py'), main)}]}


class TaskLine(unittest.TestCase):
    TOOL = 'C:\\kit\\tools\\worktree_remove.py'

    def line(self, force=False):
        return W.task_line('C:\\repo', 'demo', self.TOOL, force=force, pythonw=PYW)

    def test_the_program_is_the_base_pythonw_and_the_carrier_comes_first(self):
        line = self.line()
        self.assertTrue(line.startswith("$py = 'C:\\Py\\pythonw.exe'; "), line)
        self.assertNotIn('Get-Command', line)
        self.assertIn("-Execute $py -Argument '\"C:\\kit\\tools\\no_window.py\" python "
                      "\"C:\\kit\\tools\\worktree_remove.py\" -C \"C:\\repo\" --sweep-merged --apply'", line)

    def test_daily_as_the_user_not_elevated_and_started_when_missed(self):
        line = self.line()
        self.assertIn("Register-ScheduledTask -TaskName 'demo-worktree-sweep'", line)
        self.assertIn('-Trigger (New-ScheduledTaskTrigger -Daily -At 03:00)', line)
        self.assertIn('-Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)', line)
        self.assertIn('-User $env:USERNAME -RunLevel Limited', line)
        self.assertIn("-WorkingDirectory 'C:\\repo'", line)

    def test_a_store_alias_or_a_missing_file_throws_in_powershell(self):
        line = self.line()
        self.assertIn(T.NOT_THE_STORE_ALIAS, line)
        self.assertIn(agenttemp.NOT_A_FILE, line)

    def test_force_only_when_registering_again(self):
        self.assertNotIn('-Force', self.line())
        self.assertIn('Register-ScheduledTask -Force -TaskName', self.line(force=True))

    def test_it_is_the_worktree_tools_line_plus_the_file_check(self):
        mine = self.line().replace(agenttemp.NOT_A_FILE + '; ', '')
        self.assertEqual(mine, T.command('C:\\repo', 'demo', self.TOOL, PYW))

    def test_the_default_pythonw_is_the_base_install_literal(self):
        from kitlib.launcher import pythonw as P
        line = W.task_line('C:\\repo', 'demo', self.TOOL)
        self.assertIn("$py = %s" % T.quoted(P.base_pythonw()), line)


class RemovedExport(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='wsweep-exports-')
        self.addCleanup(gitfixture.rmtree, self.root)
        write_json(os.path.join(self.root, THERE, pluginsource.MARKER), {'commit': 'x'})

    def test_a_task_running_a_folder_with_no_marker_names_it(self):
        task = sweep_task('demo', os.path.join(self.root, GONE), 'C:\\repo')
        self.assertEqual(W.removed_export(task, self.root), GONE)

    def test_a_task_running_an_export_still_there_is_not_stale(self):
        task = sweep_task('demo', os.path.join(self.root, THERE), 'C:\\repo')
        self.assertIsNone(W.removed_export(task, self.root))

    def test_a_task_running_a_checkout_outside_the_export_root_is_not_stale(self):
        self.assertIsNone(W.removed_export(sweep_task('demo', 'C:\\kit', 'C:\\repo'), self.root))

    def test_a_folder_that_is_not_an_export_name_is_not_stale(self):
        task = sweep_task('demo', os.path.join(self.root, 'not-hex-name'), 'C:\\repo')
        self.assertIsNone(W.removed_export(task, self.root))

    def test_case_and_slashes_do_not_matter(self):
        kit = os.path.join(self.root, GONE).upper().replace('\\', '/')
        self.assertEqual(W.removed_export(sweep_task('demo', kit, 'C:\\repo'), self.root), GONE)


class ToolRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='wsweep-root-')
        self.addCleanup(gitfixture.rmtree, self.root)
        self.export = os.path.join(self.root, 'export')
        self.live = os.path.join(self.root, 'live')
        os.makedirs(os.path.join(self.live, '.git'))

    def marker(self, **data):
        write_json(os.path.join(self.export, pluginsource.MARKER), data)

    def test_an_export_names_the_live_root_it_runs_for(self):
        self.marker()
        self.assertEqual(W.tool_root(self.export, {pluginsource.LIVE_ROOT_ENV: self.live}), self.live)

    def test_an_export_names_the_checkout_its_marker_records(self):
        self.marker(checkout=self.live)
        self.assertEqual(W.tool_root(self.export, {}), self.live)

    def test_an_export_with_neither_has_no_root(self):
        self.marker()
        self.assertIsNone(W.tool_root(self.export, {}))
        self.marker(checkout=os.path.join(self.root, 'moved'))          # a checkout that is gone
        self.assertIsNone(W.tool_root(self.export, {}))

    def test_a_checkout_is_itself(self):
        self.assertEqual(W.tool_root(self.live, {pluginsource.LIVE_ROOT_ENV: self.export}), self.live)


class StepBase(MS.Base):
    """machine setup's worktree sweep step through M.run against FakeHost, in the old-kit switch
    (the default mode installs plugins, which the fake host does not), with the agent temp sweep
    task off. The kit is an export with no live root whose marker records the kit's main checkout
    (`self.checkout`, holding the tool `self.tool`); the machine config lists one repository."""

    def setUp(self):
        MS.Base.setUp(self)
        no_machine_owner(self)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(pluginsource.LIVE_ROOT_ENV, None)
        kit_repo = gitfixture.TempRepo()                               # the kit's main checkout
        self.addCleanup(kit_repo.cleanup)
        self.checkout = os.path.realpath(kit_repo.root)
        self.tool = os.path.join(self.checkout, 'tools', 'worktree_remove.py')
        MS.touch(self.tool)
        MS.touch(os.path.join(self.kit, 'tools', 'worktree_remove.py'))
        write_json(os.path.join(self.kit, pluginsource.MARKER), {'commit': 'x', 'checkout': self.checkout})
        self.exports = os.path.join(self.root, 'exports')
        write_json(os.path.join(self.exports, THERE, pluginsource.MARKER), {'commit': 'y'})
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.main = os.path.realpath(self.repo.root)
        self.name = os.path.basename(self.main)
        self.task = T.task_name(self.name)
        self.config({'demo': self.repo.root})
        self.events, self.regs, self.tasks = [], [], []
        self.dry_report = ['would remove C:\\wt\\old  landed']
        self.dry_error = None

        def task_register(line):
            self.events.append('register')
            self.regs.append(line)

        def dry_run(tool, main):
            self.events.append(('dry-run', tool, main))
            if self.dry_error:
                raise self.dry_error
            return list(self.dry_report)

        self.host.task_register = task_register
        self.host.task_list = lambda: list(self.tasks)
        self.host.worktree_sweep_dry_run = dry_run

    def config(self, repos):
        cfg = {}
        for name in (kuser.FILE_NAME, kuser.LOCAL_FILE_NAME):      # the PM repository's two files
            try:
                with open(os.path.join(self.cfg_dir, name), encoding='utf-8') as f:
                    cfg.update(json.load(f))
            except OSError:
                pass
        cfg['launcher'] = {'repos': repos}
        cfg['plugin'] = {'export_dir': self.exports}
        machine_config(self.home, cfg, case=self)

    def go(self, apply=False, **kw):
        kw.setdefault('agent_temp', os.path.join(self.root, 'new-agent-temp'))
        kw.setdefault('sweep_task', False)
        ctx = M.Context(self.home, self.kit, self.old, desktop=self.desk, host=self.host, today=MS.TODAY, **kw)
        code, lines = M.run(ctx, apply=apply)
        return code, '\n'.join(lines)


class Step(StepBase):
    def test_the_dry_run_plans_the_sweep_then_the_task_and_registers_nothing(self):
        code, out = self.go()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('run the worktree sweep once without --apply for demo: %s -C %s --sweep-merged'
                      % (self.tool, self.main), out)
        self.assertIn('register the scheduled task %s (daily 03:00, as you, not elevated): $py = ' % self.task, out)
        self.assertLess(out.index('run the worktree sweep once'), out.index('register the scheduled task %s' % self.task))
        self.assertEqual(self.events, [])

    def test_apply_runs_the_sweep_dry_run_first_then_registers(self):
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        tool = self.tool
        self.assertEqual(self.events, [('dry-run', tool, self.main), 'register'])
        self.assertIn('  worktree sweep dry run (nothing removed): would remove C:\\wt\\old  landed', out)
        line = self.regs[0]
        self.assertIn("-TaskName %s" % T.quoted(self.task), line)
        self.assertIn(T.argument(tool, self.main), line)
        self.assertNotIn('-Force', line)
        self.assertIn('done: register the scheduled task %s' % self.task, out)

    def test_a_current_task_gets_nothing(self):
        self.tasks = [sweep_task(self.name, self.checkout, self.main)]
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.events, [])
        self.assertNotIn(self.task, out)

    def test_a_task_on_an_export_still_there_is_left(self):
        self.tasks = [sweep_task(self.name, os.path.join(self.exports, THERE), self.main)]
        code, out = self.go(apply=True)
        self.assertEqual(self.events, [], out)

    def test_a_task_on_a_removed_export_is_registered_again_with_force(self):
        self.tasks = [sweep_task(self.name, os.path.join(self.exports, GONE), self.main)]
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('re-register the scheduled task %s (it runs from the removed plugin export %s; '
                      'daily 03:00, as you, not elevated)' % (self.task, os.path.join(self.exports, GONE)), out)
        self.assertEqual([e if isinstance(e, str) else e[0] for e in self.events], ['dry-run', 'register'])
        self.assertIn('Register-ScheduledTask -Force -TaskName', self.regs[0])
        self.assertNotIn(GONE, self.regs[0])

    def test_the_opt_out_is_a_note_with_the_line_and_registers_nothing(self):
        code, out = self.go(apply=True, worktree_sweep=False)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('worktree sweep task %s (demo) is not registered (--no-worktree-sweep); a run without '
                      '--no-worktree-sweep registers it, or run: $py = ' % self.task, out)
        self.assertEqual(self.events, [])

    def test_the_opt_out_leaves_a_stale_task_with_a_note(self):
        self.tasks = [sweep_task(self.name, os.path.join(self.exports, GONE), self.main)]
        code, out = self.go(apply=True, worktree_sweep=False)
        self.assertIn('worktree sweep task %s (demo) runs from the removed plugin export %s '
                      '(--no-worktree-sweep leaves it)' % (self.task, os.path.join(self.exports, GONE)), out)
        self.assertEqual(self.events, [])

    def test_a_refusing_dry_run_stops_the_run_and_registers_nothing(self):
        self.dry_error = RuntimeError('the worktree sweep dry run exit 1')
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_REFUSED, out)
        self.assertIn('STOPPED at: run the worktree sweep once without --apply for demo', out)
        self.assertEqual(self.regs, [])

    def test_a_task_list_that_cannot_be_read_is_a_note_and_plans_nothing(self):
        def boom():
            raise RuntimeError('schtasks broke')
        self.host.task_list = boom
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('worktree sweep tasks: could not read the scheduled tasks (schtasks broke); none planned', out)
        self.assertEqual(self.events, [])

    def test_each_repository_gets_its_own_task(self):
        self.config({'demo': self.repo.root, 'more': self.named('more')})
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.regs), 2, out)
        self.assertEqual(self.events.count('register'), 2)

    def test_a_repository_folder_that_is_gone_is_a_note(self):
        self.config({'demo': os.path.join(self.root, 'no-such-repo')})
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('worktree sweep task skipped for demo: no folder %s' % os.path.join(self.root, 'no-such-repo'), out)
        self.assertEqual(self.events, [])

    def test_no_repositories_no_step(self):
        self.config({})
        code, out = self.go(apply=True)
        self.assertNotIn('worktree sweep', out)
        self.assertEqual(self.events, [])

    def test_the_task_runs_the_live_root_not_the_export(self):
        live_repo = gitfixture.TempRepo()                       # the kit's live main checkout
        self.addCleanup(live_repo.cleanup)
        live = os.path.realpath(live_repo.root)
        MS.touch(os.path.join(live, 'tools', 'worktree_remove.py'))
        os.environ[pluginsource.LIVE_ROOT_ENV] = live
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn(T.argument(os.path.join(live, 'tools', 'worktree_remove.py'), self.main), self.regs[0])
        self.assertNotIn(self.kit + '\\', self.regs[0])

    def test_a_kit_in_a_branch_worktree_registers_nothing(self):
        kit = gitfixture.TempRepo()
        self.addCleanup(kit.cleanup)
        tree = kit.worktree('wt')
        MS.touch(os.path.join(tree, 'tools', 'worktree_remove.py'))
        self.assertEqual(W.tool_root(tree, {}), tree)
        plan = M.Plan()
        W.survey(plan, mock.Mock(home=self.home, kit=tree, host=self.host, worktree_sweep=True))
        self.assertEqual(plan.changes, [])
        self.assertTrue(any('not a main checkout or a plugin export' in n for n in plan.notes['extras']),
                        plan.notes)

    def test_an_export_with_no_live_main_checkout_plans_no_task(self):
        write_json(os.path.join(self.kit, pluginsource.MARKER), {'commit': 'x'})
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('worktree sweep tasks: the kit at %s is a plugin export with no live main checkout'
                      % os.path.normpath(os.path.abspath(self.kit)), out)
        self.assertIn('pass --kit <kit main checkout>; none planned', out)
        self.assertEqual(self.events, [])

    def named(self, name):
        repo = gitfixture.TempRepo()
        self.addCleanup(repo.cleanup)
        write_json(os.path.join(repo.root, '.kit.json'), {'repo': {'name': name}})
        return repo.root

    def test_two_repositories_with_one_name_get_no_task_and_a_note_naming_both(self):
        one, two = self.named('same'), self.named('Same')
        self.config({'demo': self.repo.root, 'one': one, 'two': two})
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('worktree sweep task same-worktree-sweep skipped: one (%s), two (%s) take that one name'
                      % (os.path.normpath(os.path.realpath(one)), os.path.normpath(os.path.realpath(two))), out)
        self.assertEqual(len(self.regs), 1, out)                        # demo's own task still goes in
        self.assertIn(T.quoted(self.task), self.regs[0])

    def test_two_labels_on_one_repository_get_one_task(self):
        self.config({'demo': self.repo.root, 'again': self.repo.root})
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(len(self.regs), 1, out)
        self.assertNotIn('take that one name', out)


class DryRun(unittest.TestCase):
    """worktreesweep.dry_run with its run given: the sweep without --apply, and what stops it."""
    TOOL, MAIN, PY = 'C:\\kit\\tools\\worktree_remove.py', 'C:\\repo', 'C:\\Py\\python.exe'

    def go(self, code=0, out='', err='', timeout=False):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if timeout:
                raise W.subprocess.TimeoutExpired(argv, kw.get('timeout'))
            return mock.Mock(returncode=code, stdout=out, stderr=err)
        return W.dry_run(self.TOOL, self.MAIN, self.PY, run=run), calls

    def test_the_argv_is_the_sweep_without_apply(self):
        said, calls = self.go(out='would remove C:\\wt\\a  landed\n\n')
        self.assertEqual(said, ['would remove C:\\wt\\a  landed'])
        argv, kw = calls[0]
        self.assertEqual(argv, [self.PY, self.TOOL, '-C', self.MAIN, '--sweep-merged'])
        self.assertNotIn('--apply', argv)
        self.assertEqual(kw['timeout'], W.DRY_RUN_TIMEOUT)
        self.assertEqual(kw['env']['PYTHONIOENCODING'], 'utf-8')

    def test_exit_1_raises_naming_the_repository_and_the_lock(self):
        with self.assertRaises(RuntimeError) as e:
            self.go(code=1, err='deferred: the machine lock is held')
        msg = str(e.exception)
        self.assertIn(self.MAIN, msg)
        self.assertIn('exit 1 (refused, failed, or deferred by a held lock', msg)
        self.assertIn('deferred: the machine lock is held', msg)
        self.assertIn('so the task is not registered', msg)

    def test_exit_2_raises(self):
        with self.assertRaises(RuntimeError) as e:
            self.go(code=2)
        self.assertIn('exit 2 (usage, or a refused .kit.json)', str(e.exception))
        self.assertIn('no output', str(e.exception))

    def test_a_timeout_raises(self):
        with self.assertRaises(RuntimeError) as e:
            self.go(timeout=True)
        self.assertIn('took over %s s' % W.DRY_RUN_TIMEOUT, str(e.exception))
        self.assertIn(self.MAIN, str(e.exception))


class Tool(StepBase):
    """tools/machine_setup.py's --no-worktree-sweep."""

    def setUp(self):
        StepBase.setUp(self)
        sys.path.insert(0, os.path.dirname(MS.TOOL))
        self.addCleanup(sys.path.remove, os.path.dirname(MS.TOOL))

    def cli(self, *flags):
        import machine_setup
        argv = ['--old-kit', self.old, '--kit', self.kit, '--home', self.home, '--agent-temp',
                os.path.join(self.root, 'new-agent-temp'), '--desktop', self.desk, '--no-sweep-task'] + list(flags)
        with mock.patch.object(extras, 'Host', lambda: self.host), \
                contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            code = machine_setup.main(argv)
        return code, out.getvalue()

    def test_registered_by_default_and_the_flag_opts_out(self):
        code, out = self.cli('--no-worktree-sweep', '--apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.regs, [])
        self.assertIn('(--no-worktree-sweep)', out)
        code, out = self.cli('--apply')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.regs), 1, out)

    def test_the_flag_is_machine_only(self):
        code, out = self.cli('--repo', self.repo.root, '--no-worktree-sweep')
        self.assertNotEqual(code, 0)


class PmStartCheck(unittest.TestCase):
    """kitlib.pmstart.worktree_sweep_lines with its readers given."""

    def setUp(self):
        no_machine_owner(self)
        self.repo = gitfixture.TempRepo()
        self.addCleanup(self.repo.cleanup)
        self.main = os.path.realpath(self.repo.root)
        self.task = T.task_name(os.path.basename(self.main))
        self.exports = tempfile.mkdtemp(prefix='wsweep-pm-exports-')
        self.addCleanup(gitfixture.rmtree, self.exports)
        write_json(os.path.join(self.exports, THERE, pluginsource.MARKER), {'commit': 'y'})
        self.settings = pluginsource.Settings(self.exports, False, 'origin')

    def lines(self, tasks, repos=None, plugin=None):
        def read():
            if isinstance(tasks, Exception):
                raise tasks
            return tasks
        return pmstart.worktree_sweep_lines(repos={'demo': self.repo.root} if repos is None else repos,
                                            read=read, plugin=plugin or (lambda: self.settings))

    def test_a_missing_task(self):
        self.assertEqual(self.lines([]), [
            "worktree sweep task %s (demo) is not registered: machine_setup.py --apply registers it "
            "(the owner's act)" % self.task])

    def test_a_task_on_a_removed_export(self):
        tasks = [sweep_task(os.path.basename(self.main), os.path.join(self.exports, GONE), self.main)]
        self.assertEqual(self.lines(tasks), [
            "worktree sweep task %s (demo) runs from a removed plugin export %s: machine_setup.py --apply "
            "registers it again (the owner's act)" % (self.task, GONE)])

    def test_current_tasks_give_no_line(self):
        tasks = [sweep_task(os.path.basename(self.main), os.path.join(self.exports, THERE), self.main),
                 sweep_task('unrelated', 'C:\\kit', 'C:\\x')]
        self.assertEqual(self.lines(tasks), [])

    def test_no_plugin_settings_checks_presence_only(self):
        tasks = [sweep_task(os.path.basename(self.main), os.path.join(self.exports, GONE), self.main)]
        self.assertEqual(self.lines(tasks, plugin=lambda: None), [])

    def test_a_task_list_that_cannot_be_read(self):
        self.assertEqual(self.lines(RuntimeError('schtasks broke')), [
            "could not check the worktree sweep tasks: RuntimeError: schtasks broke"])

    def test_a_repository_that_cannot_be_read(self):
        gone = os.path.join(self.exports, 'no-repo')
        out = self.lines([], repos={'demo': gone})
        self.assertEqual(len(out), 1, out)
        self.assertTrue(out[0].startswith('could not check the worktree sweep task (demo): '), out)

    def test_repositories_that_take_one_name_are_flagged_together(self):
        mains = []
        for _ in range(2):
            repo = gitfixture.TempRepo()
            self.addCleanup(repo.cleanup)
            write_json(os.path.join(repo.root, '.kit.json'), {'repo': {'name': 'same'}})
            mains.append(os.path.normpath(os.path.realpath(repo.root)))
        tasks = [sweep_task('same', 'C:\\kit', mains[0])]          # one of them registered it by hand
        out = self.lines(tasks, repos={'one': mains[0], 'two': mains[1], 'demo': self.repo.root})
        self.assertEqual(out, [
            "worktree sweep task %s (demo) is not registered: machine_setup.py --apply registers it "
            "(the owner's act)" % self.task,
            "worktree sweep task same-worktree-sweep is shared: one (%s), two (%s) take that one name, so "
            "machine_setup.py registers it for none; give each its own repo.name in .kit.json" % tuple(mains)])
        self.assertEqual(self.lines([], repos={'one': mains[0], 'again': mains[0]}), [
            "worktree sweep task same-worktree-sweep (again) is not registered: machine_setup.py --apply "
            "registers it (the owner's act)"])

    def test_no_repositories_reads_no_tasks(self):
        self.assertEqual(self.lines(AssertionError('read'), repos={}), [])

    def test_the_checklist_carries_the_line(self):
        temp_home(self)
        handoff = {"globber": lambda p: [], "exists": lambda p: True, "pm_repo": lambda c: "X:/pm"}  # one file, no strays
        fakes = dict(pm_data=lambda: None, handoff=handoff, reminders={"read": lambda p: "[]"},
                     liveness={"read": lambda p: "{}"}, stubs={"repos": {}},
                     watcher={"beat": lambda: None, "now": 0}, foreground_lock={"read": lambda: 0},
                     worktree_sweep={'repos': {'demo': self.repo.root}, 'read': lambda: [],
                                     'plugin': lambda: self.settings})
        text = pmstart.checklist({}, datetime.datetime(2026, 10, 20, 9, 0), procs=[], **fakes)
        self.assertIn("- worktree sweep task %s (demo) is not registered" % self.task, text)


if __name__ == '__main__':
    unittest.main()
