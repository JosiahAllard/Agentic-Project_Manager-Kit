"""The PM-repo pointer (kitlib.config.pointer; docs/kit-json.md "The PM repo pointer"), the
--pm-repo mode that sets it (kitlib.machinesetup.pointer, tools/machine_setup.py) and preflight's
`pointer` line (kitlib.preflight.configline.pointer_line). The registry is never read or written
here: every read goes through a fake winreg module or the test fence, and every write through a
fake registry object.
"""
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from kitlib import machinesetup, pmdata
from kitlib.config import pointer, user
from kitlib.machinesetup import pointer as mpointer
from kitlib.preflight import configline
from tests import gitfixture
from tests.platforms import windows_only
from tests.preflight.test_preflight import Fixture
from tools import machine_setup

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = 'a' * 40


class Key(object):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg(object):
    """The parts of winreg read_raw uses; `value` is (data, kind), an exception, or None (no
    value). Every OpenKey call is recorded."""
    HKEY_LOCAL_MACHINE = 'HKLM'
    KEY_READ, KEY_WOW64_64KEY = 0x20019, 0x0100
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self, value):
        self.value, self.opened = value, []

    def OpenKey(self, root, path, reserved, access):
        self.opened.append((root, path, access))
        if isinstance(self.value, BaseException):
            raise self.value
        if self.value is None:
            raise FileNotFoundError(2, 'not found')
        return Key()

    def QueryValueEx(self, key, name):
        assert name == pointer.VALUE, name
        return self.value


class Unfenced(unittest.TestCase):
    """read_raw outside the fence, on a fake winreg."""

    def read(self, value):
        fake = FakeWinreg(value)
        with mock.patch.object(user, 'fenced', return_value=False), \
                mock.patch.object(pointer, '_winreg', return_value=fake):
            got = pointer.read_raw()
        return got, fake

    def test_the_value_is_read_from_hklm_in_the_64_bit_view(self):
        got, fake = self.read(('Z:\\GitHub\\pm', FakeWinreg.REG_SZ))
        self.assertEqual(got, ('Z:\\GitHub\\pm', None))
        self.assertEqual(fake.opened, [('HKLM', r'SOFTWARE\pm-kit',
                                        FakeWinreg.KEY_READ | FakeWinreg.KEY_WOW64_64KEY)])

    def test_no_key_or_value_is_no_pointer(self):
        self.assertEqual(self.read(None)[0], (None, None))

    def test_a_registry_error_is_reported(self):
        value, why = self.read(PermissionError(5, 'Access is denied'))[0]
        self.assertIsNone(value)
        self.assertIn('cannot read HKLM\\SOFTWARE\\pm-kit pm_repo', why)

    def test_a_value_that_is_not_reg_sz_is_refused(self):
        value, why = self.read(('%X%\\pm', FakeWinreg.REG_EXPAND_SZ))[0]
        self.assertIsNone(value)
        self.assertIn('is not a REG_SZ', why)

    def test_an_empty_value_is_refused(self):
        self.assertEqual(self.read(('  ', FakeWinreg.REG_SZ))[0][1],
                         'HKLM\\SOFTWARE\\pm-kit pm_repo is empty')

    def test_the_environment_is_never_read_outside_the_fence(self):
        with mock.patch.dict(os.environ, {pointer.FENCE_ENV: 'Z:\\elsewhere'}):
            self.assertEqual(self.read(None)[0], (None, None))


class TheFence(unittest.TestCase):
    """Under the tests the registry is never opened: the value is the environment's."""

    def no_registry(self):
        return mock.patch.object(pointer, '_winreg', side_effect=AssertionError('registry read'))

    def test_the_tests_are_fenced(self):
        self.assertTrue(user.fenced())

    def test_under_the_fence_the_value_is_the_environment_s(self):
        with self.no_registry(), mock.patch.dict(os.environ, {pointer.FENCE_ENV: 'Q:\\pm'}):
            self.assertEqual(pointer.read_raw(), ('Q:\\pm', None))

    def test_under_the_fence_with_no_value_there_is_no_pointer(self):
        env = dict(os.environ)
        env.pop(pointer.FENCE_ENV, None)
        with self.no_registry(), mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(pointer.read_raw(), (None, None))
            self.assertEqual(pointer.read(), pointer.Pointer(pointer.NONE, None, None))

    def test_the_registry_is_never_written_under_the_fence(self):
        import winreg
        with mock.patch.object(winreg, 'CreateKeyEx', side_effect=AssertionError('registry write')):
            with self.assertRaises(RuntimeError) as e:
                mpointer.Registry().write('Q:\\pm')
        self.assertIn('never write the registry', str(e.exception))

    test_the_registry_is_never_written_under_the_fence = windows_only('winreg is Windows only')(
        test_the_registry_is_never_written_under_the_fence)


class Repo(unittest.TestCase):
    """A PM repository: main checkout on main, a linked data worktree on pmdata."""

    def setUp(self):
        self.repo = gitfixture.TempRepo(branch='main')
        self.addCleanup(self.repo.cleanup)
        self.root = self.repo.root
        self.data = self.repo.worktree('data', 'pmdata')

    def record(self, branch='pmdata'):
        pmdata.write(self.repo.common_dir(), branch, 'origin', ROOT)

    def fenced_value(self, value):
        return mock.patch.dict(os.environ, {pointer.FENCE_ENV: value})


class Check(Repo):

    def test_a_main_checkout_with_no_record_is_good(self):
        self.assertIsNone(pointer.check(self.root))

    def test_the_data_worktree_on_the_recorded_branch_is_good(self):
        self.record()
        self.assertIsNone(pointer.check(self.data))

    def test_a_relative_path_is_refused(self):
        self.assertIn('is not an absolute path', pointer.check('GitHub\\pm'))
        self.assertIn('is not an absolute path', pointer.check('\\GitHub\\pm'))

    def test_a_missing_folder_is_refused(self):
        self.assertIn('no folder at', pointer.check(os.path.join(self.repo.base, 'gone')))

    def test_a_folder_outside_any_repository_is_refused(self):
        plain = os.path.join(self.repo.base, 'plain')
        os.makedirs(plain)
        self.assertIn('is not a git work tree', pointer.check(plain))

    def test_a_subfolder_of_a_work_tree_is_refused(self):
        sub = os.path.join(self.root, 'docs')
        os.makedirs(sub)
        self.assertIn('is not the top of its work tree', pointer.check(sub))

    def test_a_work_tree_off_the_data_branch_is_refused(self):
        self.record()
        why = pointer.check(self.root)
        self.assertIn("is on branch 'main', not the PM data branch pmdata", why)

    def test_a_pm_slug_worktree_is_refused(self):
        self.record()
        slug = self.repo.worktree('pm-x', 'pm/x')
        self.assertIn("is on branch 'pm/x', not the PM data branch pmdata", pointer.check(slug))

    def test_a_linked_worktree_with_no_record_is_refused(self):
        self.assertIn('is a linked worktree and its repository has no PM data record',
                      pointer.check(self.data))

    def test_an_unreadable_record_is_refused(self):
        with open(os.path.join(self.repo.common_dir(), pmdata.FILE_NAME), 'w') as f:
            f.write('{')
        self.assertIn('is not valid JSON', pointer.check(self.root))

    def test_read_reports_ok_and_bad(self):
        self.record()
        with self.fenced_value(self.data):
            got = pointer.read()
        self.assertEqual(got[:3], (pointer.OK, self.data, None))
        self.assertEqual(got.record.branch, 'pmdata')      # the record read, for pm_data
        with self.fenced_value(self.root):
            got = pointer.read()
        self.assertEqual((got.state, got.path), (pointer.BAD, self.root))
        self.assertIn('not the PM data branch', got.error)


class Commands(unittest.TestCase):

    @mock.patch('kitlib.platform.NAME', 'windows')
    def test_the_reg_commands(self):
        self.assertEqual(pointer.command('Z:\\work\\pm-data'),
                         'reg add "HKLM\\SOFTWARE\\pm-kit" /v pm_repo /t REG_SZ '
                         '/d "Z:\\work\\pm-data" /f /reg:64')
        self.assertEqual(pointer.delete_command(),
                         'reg delete "HKLM\\SOFTWARE\\pm-kit" /v pm_repo /f /reg:64')

    @mock.patch('kitlib.platform.NAME', 'linux')
    def test_the_linux_commands_write_the_pointer_file(self):
        path = os.path.join(tempfile.gettempdir(), 'pm data').replace('\\', '/')
        self.assertEqual(pointer.command(path),
                         "sudo mkdir -p /etc/pm-kit && printf '%%s\\n' '%s' | sudo tee "
                         "/etc/pm-kit/pointer >/dev/null" % path)
        self.assertEqual(pointer.delete_command(), 'sudo rm -f /etc/pm-kit/pointer')
        self.assertNotIn('reg ', pointer.command(path) + pointer.delete_command())

    @mock.patch('kitlib.platform.NAME', 'linux')
    def test_where_names_the_linux_files(self):
        self.assertEqual(pointer.where(),
                         'pointer file /etc/pm-kit/pointer, else <profile>/.config/pm-kit/pointer')

    @mock.patch('kitlib.platform.NAME', 'windows')
    def test_where_names_the_registry_value_on_windows(self):
        self.assertEqual(pointer.where(), 'HKLM\\SOFTWARE\\pm-kit pm_repo')

    def test_a_trailing_backslash_does_not_escape_the_quote(self):
        self.assertIn('/d "Z:\\\\" /f', pointer.command('Z:\\'))


class Host(object):
    def __init__(self, admin):
        self.admin = admin

    def is_admin(self):
        return self.admin


class Registry(object):
    def __init__(self, value=None, why=None):
        self.value, self.why, self.written = value, why, []

    def read(self):
        return self.value, self.why

    def write(self, value):
        self.written.append(value)


@mock.patch('kitlib.platform.NAME', 'windows')
class Setup(Repo):
    """--pm-repo through machinesetup.run_pointer, on a fake host and registry."""

    def run_it(self, path, admin=False, apply=False, registry=None):
        self.reg = registry or Registry()
        ctx = mpointer.PointerContext(path, KIT, host=Host(admin), registry=self.reg)
        return machinesetup.run_pointer(ctx, apply=apply)

    def test_a_dry_run_prints_the_change_and_the_reg_add(self):
        code, lines = self.run_it(self.root)
        self.assertEqual(code, machinesetup.EXIT_OK, lines)
        self.assertIn('  HKLM\\SOFTWARE\\pm-kit pm_repo: set it to %s' % self.root, lines)
        self.assertIn('    by hand, from an administrator terminal: %s' % pointer.command(self.root), lines)
        self.assertIn('  note: not elevated: --apply is refused here; run it from an administrator '
                      'terminal', lines)
        self.assertIn('pm-repo: 1 change(s) to make; re-run with --apply to make them', lines)
        self.assertEqual(self.reg.written, [])

    def test_apply_is_refused_unless_elevated(self):
        code, lines = self.run_it(self.root, apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, lines)
        refused = [l for l in lines if 'REFUSED' in l]
        self.assertEqual(len(refused), 1, lines)
        self.assertIn('not elevated', refused[0])
        self.assertIn(pointer.command(self.root), refused[0])
        self.assertEqual(self.reg.written, [])

    def test_apply_elevated_writes_the_value(self):
        code, lines = self.run_it(self.root, admin=True, apply=True)
        self.assertEqual(code, machinesetup.EXIT_OK, lines)
        self.assertEqual(self.reg.written, [self.root])
        self.assertIn('done: HKLM\\SOFTWARE\\pm-kit pm_repo: set it to %s' % self.root, lines)

    def test_the_value_is_normalised_before_it_is_written(self):
        self.run_it(self.root.replace('\\', '/') + '/', admin=True, apply=True)
        self.assertEqual(self.reg.written, [os.path.normpath(self.root)])

    def test_a_value_already_set_is_nothing_to_do(self):
        code, lines = self.run_it(self.root, admin=True, apply=True, registry=Registry(self.root))
        self.assertEqual(code, machinesetup.EXIT_OK, lines)
        self.assertIn('pm-repo: nothing to do', lines)
        self.assertEqual(self.reg.written, [])

    def test_a_replace_names_the_old_value_and_how_to_restore_it(self):
        self.record()
        code, lines = self.run_it(self.data, admin=True, apply=False, registry=Registry(self.root))
        self.assertEqual(code, machinesetup.EXIT_OK, lines)
        self.assertIn('  HKLM\\SOFTWARE\\pm-kit pm_repo: replace %s with %s' % (self.root, self.data), lines)
        plan = mpointer.survey(mpointer.PointerContext(self.data, KIT, Host(True), Registry(self.root)))
        self.assertEqual(plan.changes[0].undo, pointer.command(self.root))

    def test_a_new_value_is_undone_by_reg_delete(self):
        plan = mpointer.survey(mpointer.PointerContext(self.root, KIT, Host(True), Registry()))
        self.assertEqual(plan.changes[0].undo, pointer.delete_command())

    def test_an_unreadable_old_value_is_never_restored_by_reg_delete(self):
        reg = Registry(None, 'is not a REG_SZ (type 2)')
        plan = mpointer.survey(mpointer.PointerContext(self.root, KIT, Host(True), reg))
        self.assertIsNone(plan.changes[0].undo)
        reg.write = mock.Mock(side_effect=OSError('denied'))
        ctx = mpointer.PointerContext(self.root, KIT, host=Host(True), registry=reg)
        code, lines = machinesetup.run_pointer(ctx, apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, lines)
        self.assertTrue(any('STOPPED' in l for l in lines), lines)
        self.assertFalse(any('reg delete' in l or 'restore:' in l for l in lines), lines)
        self.assertTrue(any('cannot restore it' in l for l in lines), lines)

    def test_the_refusal_command_doubles_a_trailing_backslash(self):
        self.assertIn(r'--pm-repo "Z:\\"', mpointer.command(KIT, 'Z:' + chr(92)))
        self.assertIn(r'--pm-repo "Z:\work"', mpointer.command(KIT, r'Z:\work'))

    def test_a_bad_target_is_refused_before_anything(self):
        self.record()
        code, lines = self.run_it(self.root, admin=True, apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, lines)
        self.assertTrue(any('REFUSED - --pm-repo: ' in l and 'not the PM data branch' in l for l in lines),
                        lines)
        self.assertEqual(self.reg.written, [])

    def test_an_unreadable_current_value_is_noted_and_replaced(self):
        code, lines = self.run_it(self.root, admin=True, apply=True,
                                  registry=Registry(None, 'is not a REG_SZ (type 2)'))
        self.assertEqual(code, machinesetup.EXIT_OK, lines)
        self.assertIn('  note: the value there now cannot be read (is not a REG_SZ (type 2)): it is '
                      'replaced, and this tool cannot restore it; note it first, e.g. with '
                      '`reg query "HKLM\\SOFTWARE\\pm-kit" /v pm_repo`', lines)
        self.assertEqual(self.reg.written, [self.root])

    def test_a_folder_that_is_not_a_kit_is_refused(self):
        ctx = mpointer.PointerContext(self.root, self.repo.base, Host(True), Registry())
        code, lines = machinesetup.run_pointer(ctx, apply=True)
        self.assertEqual(code, machinesetup.EXIT_REFUSED, lines)
        self.assertTrue(any('is not a kit checkout' in l for l in lines), lines)


class Cli(Repo):
    """tools/machine_setup.py --pm-repo: options and exit codes."""

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(mpointer, 'Registry', Registry):
            code = machine_setup.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_other_options_are_usage_errors(self):
        for extra in (['--owner-email', 'x@example.invalid'], ['--home', self.repo.base],
                      ['--agent-email', 'y@example.invalid'], ['--managed-settings']):
            code, _out, err = self.main('--pm-repo', self.root, *extra)
            self.assertEqual(code, machinesetup.EXIT_USAGE, (extra, err))

    def test_a_dry_run_exits_0(self):
        code, out, _err = self.main('--pm-repo', self.root)
        self.assertEqual(code, 0, out)
        self.assertIn('pm-repo: DRY RUN', out)
        self.assertIn(pointer.command(self.root), out)

    def test_a_bad_path_exits_1(self):
        code, out, _err = self.main('--pm-repo', 'relative\\pm')
        self.assertEqual(code, machinesetup.EXIT_REFUSED, out)
        self.assertIn('is not an absolute path', out)


class PreflightLine(Fixture):
    """The `pointer` line in preflight's output (kitlib.preflight.report)."""

    def run_with(self, value):
        env = dict(os.environ)
        env.pop(pointer.FENCE_ENV, None)
        if value is not None:
            env[pointer.FENCE_ENV] = value
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(pointer, '_winreg', side_effect=AssertionError('registry read')):
            return self.run_preflight()

    def test_the_line_names_the_pm_repo(self):
        code, out = self.run_with(self.root)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.line(out, 'pointer '), 'pointer   %s' % self.root.replace('\\', '/'))

    def test_the_line_comes_after_the_config_line(self):
        out = self.run_with(self.root)[1].splitlines()
        config = [i for i, l in enumerate(out) if l.startswith('config ')]
        self.assertEqual(out[config[0] + 1], 'pointer   %s' % self.root.replace('\\', '/'))

    @mock.patch('kitlib.platform.NAME', 'windows')
    def test_with_no_pointer_the_line_says_none_and_how_to_set_it(self):
        line = self.line(self.run_with(None)[1], 'pointer ')
        self.assertTrue(line.startswith('pointer   none - no PM-repo pointer (HKLM\\SOFTWARE\\pm-kit '
                                        'pm_repo)'), line)
        self.assertIn('machine_setup.py --pm-repo', line)

    @mock.patch('kitlib.platform.NAME', 'windows')
    def test_a_bad_pointer_is_broken(self):
        line = self.line(self.run_with(os.path.join(self.repo.base, 'gone'))[1], 'pointer ')
        self.assertTrue(line.startswith('pointer   BROKEN HKLM\\SOFTWARE\\pm-kit pm_repo: no folder at'),
                        line)

    def test_pointer_line_is_what_the_report_prints(self):
        with mock.patch.object(configline, 'pointer_line', return_value='pointer   marker'):
            out = self.run_preflight()[1]
        self.assertIn('pointer   marker', out.splitlines())


class PointerLineWording(unittest.TestCase):
    """configline.pointer_line names the registry value on Windows and the pointer files on Linux."""

    def line(self, name, state, error=None):
        with mock.patch('kitlib.platform.NAME', name),                 mock.patch.object(pointer, 'read', return_value=pointer.Pointer(state, None, error)):
            return configline.pointer_line()

    def test_none_on_windows_names_the_registry_value(self):
        self.assertIn('no PM-repo pointer (HKLM\\SOFTWARE\\pm-kit pm_repo);',
                      self.line('windows', pointer.NONE))

    def test_none_on_linux_names_the_pointer_files(self):
        line = self.line('linux', pointer.NONE)
        self.assertIn('no PM-repo pointer (pointer file /etc/pm-kit/pointer, else '
                      '<profile>/.config/pm-kit/pointer);', line)
        self.assertNotIn('HKLM', line)

    def test_broken_on_windows_names_the_registry_value(self):
        self.assertEqual(self.line('windows', pointer.BAD, 'why'),
                         'pointer   BROKEN HKLM\\SOFTWARE\\pm-kit pm_repo: why')

    def test_broken_on_linux_names_the_pointer_files(self):
        self.assertEqual(self.line('linux', pointer.BAD, 'why'),
                         'pointer   BROKEN pointer file /etc/pm-kit/pointer, else '
                         '<profile>/.config/pm-kit/pointer: why')


if __name__ == '__main__':
    unittest.main()
