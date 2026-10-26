"""kitlib.machinesetup.kitcommand: the kit command's launcher (kit.cmd in <plugin.export_dir>\\bin) and
that folder on the user Path, planned by machine setup. The user Path is a fake host's value:
nothing here reads or writes the real registry, and the one launcher that runs is in a temp folder.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup as M
from kitlib.machinesetup import kitcommand as K
from tests import gitfixture
from tests.machinesetup import test_machinesetup as MS
from tests.machinesetup import test_worktree_sweep_task as WS
from tests.pmstart import keep_off_launcher_repos
from tests.platforms import windows_only

ROOT = 'C:\\kit main'
RAW = '%USERPROFILE%\\bin;C:\\tools;'


def setUpModule():
    keep_off_launcher_repos()


class PathHost(object):
    """The user Path side of the host: `raw` and `kind` are the value; `writes` records each write."""

    def __init__(self, raw=RAW, kind=K.REG_EXPAND_SZ):
        self.raw, self.kind, self.writes, self.reads = raw, kind, [], 0

    def user_path(self):
        self.reads += 1
        return self.raw, (self.kind if self.raw is not None else None)

    def set_user_path(self, value, kind):
        self.writes.append((value, kind))
        self.raw, self.kind = value, kind


def carry_out(plan, apply):
    lines = []
    code, _ = M._carry_out('machine-setup', plan, apply, lambda *ls: lines.extend(ls), [])
    return code, '\n'.join(lines)


class LauncherText(unittest.TestCase):
    def test_three_crlf_lines_running_the_checkouts_kit_py_with_every_argument(self):
        text = K.launcher_text(ROOT)
        self.assertEqual(text.split('\r\n'), ['@echo off', text.split('\r\n')[1],
                                              'python "C:\\kit main\\tools\\kit.py" %*', ''])
        self.assertTrue(text.split('\r\n')[1].startswith(K.MARK))
        self.assertNotIn('\n', text.replace('\r\n', ''))

    def test_a_percent_in_the_path_is_doubled(self):
        self.assertIn('python "C:\\50%%\\tools\\kit.py" %*', K.launcher_text('C:\\50%'))


class PathValue(unittest.TestCase):
    def test_a_listed_folder_is_found_whatever_its_spelling(self):
        for raw in ('C:\\a;C:\\Kit\\Bin', 'c:/kit/bin/;C:\\a', '"C:\\kit\\bin"', 'C:\\a;;C:\\KIT\\bin;'):
            with self.subTest(raw=raw):
                self.assertTrue(K.path_has(raw, 'C:\\kit\\bin'))
        self.assertFalse(K.path_has('C:\\kit\\bin2;C:\\kit', 'C:\\kit\\bin'))
        self.assertFalse(K.path_has(None, 'C:\\kit\\bin'))

    def test_a_var_entry_is_matched_expanded(self):
        with mock.patch.dict(os.environ, {'KITTESTDIR': 'C:\\kit'}):
            self.assertTrue(K.path_has('%KITTESTDIR%\\bin', 'C:\\kit\\bin'))

    def test_the_folder_is_appended_and_every_entry_kept_raw(self):
        self.assertEqual(K.path_with(RAW, 'C:\\k\\bin'), '%USERPROFILE%\\bin;C:\\tools;C:\\k\\bin')
        self.assertEqual(K.path_with('', 'C:\\k\\bin'), 'C:\\k\\bin')
        self.assertEqual(K.path_with(None, 'C:\\k\\bin'), 'C:\\k\\bin')


class Steps(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='kitcmd-setup-')
        self.addCleanup(gitfixture.rmtree, self.tmp)
        self.folder = os.path.join(self.tmp, 'plugin', 'bin')
        self.path = os.path.join(self.folder, K.FILE_NAME)
        self.host = PathHost()

    def plan(self):
        plan = M.Plan()
        K.plan_steps(plan, self.host, ROOT, self.folder)
        return plan

    def test_the_dry_run_plans_both_reads_the_path_and_writes_nothing(self):
        code, out = carry_out(self.plan(), apply=False)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('write the kit command %s: python "C:\\kit main\\tools\\kit.py" %%*' % self.path, out)
        self.assertIn('add %s to the user PATH (HKCU\\Environment Path, appended' % self.folder, out)
        self.assertIn('2 change(s) to make', out)
        self.assertFalse(os.path.exists(self.folder))
        self.assertEqual((self.host.writes, self.host.reads), ([], 1))

    def test_apply_writes_the_launcher_and_appends_the_folder_keeping_the_type(self):
        code, out = carry_out(self.plan(), apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        with open(self.path, 'rb') as f:
            self.assertEqual(f.read(), K.launcher_text(ROOT).encode('ascii'))
        self.assertEqual(self.host.writes, [(RAW.rstrip(';') + ';' + self.folder, K.REG_EXPAND_SZ)])

    def test_a_second_run_plans_nothing(self):
        carry_out(self.plan(), apply=True)
        plan = self.plan()
        self.assertEqual(plan.changes, [])
        self.assertEqual(len(self.host.writes), 1)

    def test_a_plain_string_path_stays_a_plain_string(self):
        self.host = PathHost('C:\\a', K.REG_SZ)
        carry_out(self.plan(), apply=True)
        self.assertEqual(self.host.writes, [('C:\\a;' + self.folder, K.REG_SZ)])

    def test_an_unset_path_becomes_the_folder_as_an_expandable_string(self):
        self.host = PathHost(None)
        carry_out(self.plan(), apply=True)
        self.assertEqual(self.host.writes, [(self.folder, K.REG_EXPAND_SZ)])

    def test_stale_text_is_rewritten_and_a_listed_folder_left(self):
        MS.touch(self.path, '%s\r\npython "C:\\moved\\tools\\kit.py" %%*\r\n' % K.MARK)
        self.host = PathHost('C:\\a;' + self.folder)
        plan = self.plan()
        self.assertEqual([c.text.split(' ')[0] for c in plan.changes], ['rewrite'])
        carry_out(plan, apply=True)
        with open(self.path, 'rb') as f:
            self.assertIn(b'C:\\kit main\\tools\\kit.py', f.read())
        self.assertEqual(self.host.writes, [])

    def test_a_kit_cmd_machine_setup_did_not_write_is_left_and_the_path_not_touched(self):
        MS.touch(self.path, '@echo off\r\necho mine\r\n')
        plan = self.plan()
        self.assertEqual(plan.changes, [])
        self.assertTrue(any('was not written by machine setup' in n for n in plan.notes['extras']), plan.notes)
        self.assertEqual(self.host.reads, 0)

    def test_a_path_that_cannot_be_read_is_a_note_and_the_launcher_still_planned(self):
        for error in (PermissionError(5, 'Access is denied'),
                      ValueError('HKCU\\Environment Path is not a string (type 7)')):
            with self.subTest(error=error):
                self.host = PathHost()
                self.host.user_path = mock.Mock(side_effect=error)
                plan = self.plan()
                self.assertEqual([c.text.split(' ')[0] for c in plan.changes], ['write'])
                self.assertTrue(any('could not be read (%s); its step is skipped' % error in n
                                    for n in plan.notes['extras']), plan.notes)
                code, out = carry_out(plan, apply=True)
                self.assertEqual(code, M.EXIT_OK, out)
                self.assertTrue(os.path.isfile(self.path))
                self.assertEqual(self.host.writes, [])
                os.remove(self.path)

    def test_a_path_that_is_not_ascii_plans_nothing(self):
        plan = M.Plan()
        K.plan_steps(plan, self.host, 'C:\\k\u00e9t', self.folder)
        self.assertEqual(plan.changes, [])
        self.assertTrue(any('not plain ASCII' in n for n in plan.notes['extras']), plan.notes)


@windows_only('kit.cmd runs under cmd.exe')
class Launcher(unittest.TestCase):
    def test_the_launcher_runs_kit_py_with_the_arguments_and_gives_its_exit_code(self):
        tmp = tempfile.mkdtemp(prefix='kitcmd-run-')
        self.addCleanup(gitfixture.rmtree, tmp)
        root = os.path.join(tmp, 'kit 50%')
        MS.touch(os.path.join(root, 'tools', 'kit.py'),
                 'import os, sys\nprint(repr(sys.argv[1:]), os.getcwd())\nsys.exit(7)\n')
        path = os.path.join(tmp, 'bin', K.FILE_NAME)
        K.write_launcher(path, K.launcher_text(root))
        env = dict(os.environ, PATH=os.path.dirname(sys.executable) + os.pathsep + os.environ.get('PATH', ''))
        r = subprocess.run([path, 'gate', '-C', 'a b'], cwd=tmp, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 7, r.stderr)
        self.assertEqual(r.stdout.strip(), "['gate', '-C', 'a b'] %s" % tmp)


@windows_only('HKCU is Windows')
class RealPathRead(unittest.TestCase):
    def test_the_real_user_path_reads_as_a_string_of_a_string_type_or_unset(self):
        """Reads HKCU\\Environment Path only; nothing is written."""
        from kitlib.machinesetup import extras
        with mock.patch.object(extras.Host, 'set_user_path', side_effect=AssertionError('no write')):
            raw, kind = extras.Host().user_path()
        if raw is None:
            self.assertIsNone(kind)
        else:
            self.assertIsInstance(raw, str)
            self.assertIn(kind, (K.REG_SZ, K.REG_EXPAND_SZ))


class Survey(WS.StepBase):
    """Through M.run against the fake host of test_machinesetup: the kit is an export whose marker
    records a main checkout (WS.StepBase), and plugin.export_dir is `self.exports`."""

    def setUp(self):
        WS.StepBase.setUp(self)
        self.config({})
        self.paths = PathHost()
        self.host.user_path = self.paths.user_path
        self.host.set_user_path = self.paths.set_user_path
        self.folder = os.path.join(self.exports, K.FOLDER)

    def test_a_main_checkout_with_tools_kit_py_gets_the_launcher_in_the_export_folders_bin(self):
        MS.touch(os.path.join(self.checkout, 'tools', 'kit.py'))
        code, out = self.go()
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn('write the kit command %s: python "%s" %%*'
                      % (os.path.join(self.folder, K.FILE_NAME), os.path.join(self.checkout, 'tools', 'kit.py')), out)
        self.assertIn('add %s to the user PATH' % self.folder, out)
        self.assertEqual(self.paths.writes, [])
        code, out = self.go(apply=True)
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertTrue(os.path.isfile(os.path.join(self.folder, K.FILE_NAME)))
        self.assertEqual(len(self.paths.writes), 1)
        code, out = self.go()
        self.assertNotIn('kit command', out)
        self.assertNotIn('user PATH', out)

    def test_no_tools_kit_py_is_a_note(self):
        code, out = self.go()
        self.assertIn('kit command: no %s; not planned' % os.path.join(self.checkout, 'tools', 'kit.py'), out)
        self.assertEqual(self.paths.reads, 0)

    def test_a_kit_in_a_branch_worktree_plans_nothing(self):
        kit = gitfixture.TempRepo()
        self.addCleanup(kit.cleanup)
        tree = kit.worktree('wt')
        MS.touch(os.path.join(tree, 'tools', 'kit.py'))
        plan = M.Plan()
        K.survey(plan, mock.Mock(home=self.home, kit=tree, host=self.host))
        self.assertEqual(plan.changes, [])
        self.assertTrue(any('is not a main checkout' in n for n in plan.notes['extras']), plan.notes)
        self.assertEqual(self.paths.reads, 0)


if __name__ == '__main__':
    unittest.main()
