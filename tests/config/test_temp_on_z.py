"""kitlib.config.user.work_temp keeps the kit's churn (test run folders, fixture repositories)
out of every folder sessions use as TEMP (session_temps), and make_temp falls back to the kit's
default folder (default_work_temp) before this process's TEMP. Every test gives its home, its
environment, platform 'win32' and a registry reader: nothing reads this machine's TEMP, AGENT_TEMP
or registry."""
import ast
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from kitlib.config import user
from kitlib.config.types import ConfigError
from kitlib.worktree import sweep
from tests.worktree import test_worktree_sweep

HERE = os.path.dirname(os.path.abspath(__file__))
DRIVE = os.path.splitdrive(HERE)[0]
ROOT = DRIVE + os.sep


def other_letter(*avoid):
    """A drive letter none of `avoid` uses; the drive need not exist."""
    used = {a[0].upper() for a in avoid if a}
    return next(c for c in 'QRSTUVWXYJKLMNOP' if c not in used) + ':'


def reader(values):
    """A registry reader (name -> value or None) over the dict `values`."""
    return lambda name: values.get(name)


def refuse(name):
    raise AssertionError('the registry was read for %s' % name)


@unittest.skipUnless(os.name == 'nt' and DRIVE, 'drive letters are Windows only')
class WorkTempAvoidsSessionTemps(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        # A shared temp folder on another drive, so the checkout's drive root is a candidate.
        self.env = {'LOCALAPPDATA': other_letter(DRIVE) + os.sep + 'lad'}
        self.kit_temp = os.path.join(ROOT, user.TEMP_DIR_NAME)
        self.kit_work = os.path.join(ROOT, user.WORK_DIR_NAME)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)

    def temp(self, reg, env=None):
        return user.work_temp(HERE, home=self.home, environ=self.env if env is None else env,
                              platform='win32', reg=reader(reg))

    def test_a_temp_dir_that_is_the_agents_temp_gives_the_drive_roots_kit_temp(self):
        agent = os.path.join(ROOT, 'AgentTemp')
        self.write({'temp_dir': agent})
        self.assertEqual(self.temp({'AGENT_TEMP': agent}), self.kit_temp)

    def test_a_temp_dir_that_is_the_sweeps_folder_is_skipped_too(self):
        agent = os.path.join(ROOT, 'AgentTemp')
        self.write({'temp_dir': agent, 'agent_temp_sweep': {'dir': agent}})
        self.assertEqual(self.temp({}), self.kit_temp)

    def test_a_temp_dir_that_holds_or_is_inside_a_session_temp_is_skipped(self):
        session = os.path.join(ROOT, 'AgentTemp', 'claude')
        for configured in (os.path.join(ROOT, 'AgentTemp'),
                           os.path.join(session, 'kit'), session.upper()):
            self.write({'temp_dir': configured})
            self.assertEqual(self.temp({'TEMP': session}), self.kit_temp, configured)

    def test_a_temp_dir_that_only_shares_a_name_prefix_is_kept(self):
        configured = os.path.join(ROOT, 'AgentTempKit')
        self.write({'temp_dir': configured})
        self.assertEqual(self.temp({'AGENT_TEMP': os.path.join(ROOT, 'AgentTemp')}), configured)

    def test_no_config_and_the_agents_temp_at_kit_temp_gives_kit_work(self):
        """The fresh machine: machine setup's default agent temp is <drive>\\kit-temp."""
        self.assertEqual(self.temp({'AGENT_TEMP': self.kit_temp}), self.kit_work)

    def test_a_session_temp_at_the_drive_root_moves_it_to_the_system_temp(self):
        self.assertEqual(self.temp({'TMP': ROOT}), user.system_temp(self.env, 'win32'))

    def test_the_system_temp_taken_too_gives_kit_work_beside_it(self):
        shared = user.system_temp(self.env, 'win32')
        self.assertEqual(self.temp({'TMP': ROOT, 'TEMP': shared}),
                         os.path.join(os.path.dirname(shared), user.WORK_DIR_NAME))

    def test_every_candidate_taken_gives_the_first(self):
        lad = self.env['LOCALAPPDATA']
        self.write({'temp_dir': os.path.join(ROOT, 'AgentTemp')})
        got = self.temp({'TMP': ROOT, 'TEMP': lad})
        self.assertEqual(got, os.path.join(ROOT, 'AgentTemp'))

    def test_a_session_temp_on_another_drive_changes_nothing(self):
        far = other_letter(DRIVE, self.env['LOCALAPPDATA']) + os.sep + 'AgentTemp'
        self.assertEqual(self.temp({'AGENT_TEMP': far, 'TEMP': far}), self.kit_temp)

    def test_this_processs_temp_and_agent_temp_are_never_read(self):
        env = dict(self.env, TEMP=self.kit_temp, TMP=self.kit_temp, AGENT_TEMP=self.kit_temp)
        self.assertEqual(self.temp({}, env), self.kit_temp)


@unittest.skipUnless(os.name == 'nt' and DRIVE, 'drive letters are Windows only')
class DefaultWorkTemp(unittest.TestCase):

    def test_it_ignores_temp_dir_and_avoids_the_extra_folders(self):
        env = {'LOCALAPPDATA': other_letter(DRIVE) + os.sep + 'lad'}
        kit_temp = os.path.join(ROOT, user.TEMP_DIR_NAME)
        got = user.default_work_temp(HERE, environ=env, platform='win32', reg=reader({}))
        self.assertEqual(got, kit_temp)
        got = user.default_work_temp(HERE, environ=env, platform='win32', reg=reader({}),
                                     sessions=(kit_temp.lower(), None))
        self.assertEqual(got, os.path.join(ROOT, user.WORK_DIR_NAME))

    def test_it_avoids_the_registrys_session_temps(self):
        env = {'LOCALAPPDATA': other_letter(DRIVE) + os.sep + 'lad'}
        got = user.default_work_temp(HERE, environ=env, platform='win32',
                                     reg=reader({'AGENT_TEMP': os.path.join(ROOT, 'kit-temp')}))
        self.assertEqual(got, os.path.join(ROOT, user.WORK_DIR_NAME))


class SessionTemps(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)

    def test_the_sweeps_folder_then_the_user_environment_normalised_once(self):
        a = os.path.join(self.home, 'Agent')
        self.write({'agent_temp_sweep': {'dir': a + os.sep}})
        got = user.session_temps(self.home, reg=reader({
            'AGENT_TEMP': a.upper(), 'TEMP': os.path.join(self.home, 'desk', '.'),
            'TMP': os.path.join(self.home, 'desk')}))
        self.assertEqual(got, [os.path.normcase(a), os.path.normcase(os.path.join(self.home,
                                                                                 'desk'))])

    def test_empty_relative_and_unset_values_are_dropped(self):
        self.write({'agent_temp_sweep': {'dir': 'relative'}})
        self.assertEqual(user.session_temps(self.home, reg=reader({'AGENT_TEMP': '',
                                                                   'TEMP': 'rel'})), [])

    def test_a_fenced_process_reads_no_registry_and_no_config_without_a_home(self):
        self.assertTrue(user.fenced())
        with mock.patch.object(user, '_user_env', refuse), \
                mock.patch.object(user, 'user_config', side_effect=AssertionError('config')):
            self.assertEqual(user.session_temps(), [])

    def test_an_unfenced_process_reads_the_user_environment(self):
        seen = []

        def fake(name):
            seen.append(name)
            return None
        with mock.patch.object(user, 'fenced', return_value=False), \
                mock.patch.object(user, '_user_env', fake):
            user.session_temps(self.home)
        self.assertEqual(seen, list(user.SESSION_VARS))

    def test_a_config_that_cannot_be_read_counts_as_none(self):
        self.write({'temp_dir': 'relative'})
        self.assertEqual(user.session_temps(self.home, reg=reader({})), [])

    def test_user_env_expands_an_expandable_value(self):
        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        import winreg
        with mock.patch.object(winreg, 'OpenKey', return_value=Key()), \
                mock.patch.object(winreg, 'QueryValueEx',
                                  return_value=('%KIT_T_X%\\t', winreg.REG_EXPAND_SZ)), \
                mock.patch.object(winreg, 'ExpandEnvironmentStrings',
                                  side_effect=lambda v: v.replace('%KIT_T_X%', 'Q:\\x')):
            self.assertEqual(user._user_env('TEMP'), 'Q:\\x\\t')
        with mock.patch.object(winreg, 'OpenKey', side_effect=OSError('gone')):
            self.assertIsNone(user._user_env('TEMP'))

    if os.name != 'nt':
        test_user_env_expands_an_expandable_value = unittest.skip('winreg is Windows only')(
            test_user_env_expands_an_expandable_value)


class MakeTempFallback(unittest.TestCase):

    def setUp(self):
        self.base = tempfile.mkdtemp(prefix='kit-mt-')
        self.addCleanup(shutil.rmtree, self.base, True)

    def test_a_work_temp_that_cannot_be_used_falls_back_to_the_default_with_a_note(self):
        fallback = os.path.join(self.base, 'default')
        for boom in (OSError('access denied'), RuntimeError('no LOCALAPPDATA'),
                     ConfigError('bad temp_dir')):
            with mock.patch.object(user, 'work_temp', side_effect=boom), \
                    mock.patch.object(user, 'default_work_temp', return_value=fallback):
                folder, note = user.make_temp(self.base, 'kit-x-')
            self.assertEqual(os.path.dirname(folder), fallback)
            self.assertIn(str(boom), note)
            self.assertIn('is in %s' % fallback, note)

    def test_a_default_that_is_the_failed_folder_is_not_tried_again(self):
        parent = os.path.join(self.base, 'parent')
        real = os.makedirs

        def makedirs(path, *a, **k):
            if os.path.normcase(path) == os.path.normcase(parent):
                raise OSError('access denied')
            return real(path, *a, **k)
        tries = []
        with mock.patch.object(user, 'work_temp', return_value=parent), \
                mock.patch.object(user, 'default_work_temp',
                                  side_effect=lambda *a, **k: tries.append(1) or parent), \
                mock.patch('os.makedirs', makedirs):
            folder, note = user.make_temp(self.base, 'kit-x-')
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(tries, [1])
        self.assertEqual(os.path.normcase(os.path.dirname(folder)),
                         os.path.normcase(tempfile.gettempdir()))
        self.assertIn("this process's temp folder: %s could not be used (access denied)"
                      % parent, note)


KIT = os.path.dirname(os.path.dirname(HERE))


class HomeConfig(unittest.TestCase):
    """A temp home whose machine config this test writes."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, body):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(body, f)


class SharedTempFollowsTempDir(HomeConfig):
    """shared_temp is the configured temp_dir (temp_parent), else system_temp: the old
    %LOCALAPPDATA%\\Temp rule."""

    ENV = {'LOCALAPPDATA': r'C:\lad'}

    def test_system_temp_is_the_old_rule(self):
        self.assertEqual(user.system_temp(self.ENV, 'win32'), os.path.join(r'C:\lad', 'Temp'))
        with self.assertRaises(RuntimeError):
            user.system_temp({}, 'win32')
        with mock.patch.object(tempfile, 'gettempdir', return_value='/tmp/x'):
            self.assertEqual(user.system_temp({}, 'linux'), '/tmp/x')

    def test_a_set_temp_dir_is_the_shared_temp(self):
        configured = os.path.join(self.home, 'kit-shared')
        self.write({'temp_dir': configured})
        self.assertEqual(user.temp_parent(self.home), configured)
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home), configured)

    def test_even_when_it_is_a_session_temp(self):
        """Only work_temp keeps out of session temps; the shared temp is what every process
        agrees on, and its users are a few files."""
        configured = os.path.join(self.home, 'AgentTemp')
        self.write({'temp_dir': configured, 'agent_temp_sweep': {'dir': configured}})
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home), configured)

    def test_unset_gives_the_system_temp(self):
        self.write({})
        self.assertIsNone(user.temp_parent(self.home))
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home),
                         os.path.join(r'C:\lad', 'Temp'))

    def test_a_config_that_cannot_be_read_gives_the_system_temp_and_no_raise(self):
        self.write({'temp_dir': 'relative'})
        self.assertIsNone(user.temp_parent(self.home))
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home),
                         os.path.join(r'C:\lad', 'Temp'))

    @unittest.skipUnless(os.name == 'nt' and DRIVE, 'drive letters are Windows only')
    def test_a_temp_dir_on_a_drive_that_is_not_there_gives_the_system_temp(self):
        gone = next(c + ':' for c in 'QRSTUVWXYJKLMNOP' if not os.path.isdir(c + ':' + os.sep))
        self.write({'temp_dir': gone + os.sep + 'kit'})
        self.assertIsNone(user.temp_parent(self.home))
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home),
                         os.path.join(r'C:\lad', 'Temp'))

    def test_a_fenced_process_given_no_home_reads_no_config(self):
        self.assertTrue(user.fenced())
        with mock.patch.object(user, 'user_config', side_effect=AssertionError('config read')):
            self.assertIsNone(user.temp_parent())
            self.assertEqual(user.shared_temp(self.ENV, 'win32'),
                             os.path.join(r'C:\lad', 'Temp'))

    @unittest.skipUnless(os.name == 'nt' and DRIVE, 'drive letters are Windows only')
    def test_work_temps_own_candidates_never_take_the_shared_temp(self):
        """work_temp's chain past temp_dir uses the system temp, so a temp_dir it skipped as a
        session temp never comes back through shared_temp."""
        agent = os.path.join(ROOT, 'AgentTemp')
        self.write({'temp_dir': agent})
        env = {'LOCALAPPDATA': other_letter(DRIVE) + os.sep + 'lad'}
        with mock.patch.object(user, 'temp_parent', return_value=agent):
            got = user.work_temp(HERE, home=self.home, environ=env, platform='win32',
                                 reg=reader({'AGENT_TEMP': agent, 'TMP': ROOT}))
        self.assertEqual(got, user.system_temp(env, 'win32'))


class SharedTempMakesItsFolder(HomeConfig):
    """shared_temp makes a configured temp_dir that is not there yet (its users open files in it
    straight away), and falls back to system_temp when it cannot be made."""

    ENV = {'LOCALAPPDATA': r'C:\lad'}

    def test_a_missing_temp_dir_on_a_drive_that_is_there_is_made(self):
        configured = os.path.join(self.home, 'not-yet', 'kit')
        self.write({'temp_dir': configured})
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home), configured)
        self.assertTrue(os.path.isdir(configured))

    def test_one_that_cannot_be_made_gives_the_system_temp(self):
        blocker = os.path.join(self.home, 'a-file')
        with open(blocker, 'w') as f:
            f.write('x')
        self.write({'temp_dir': os.path.join(blocker, 'below')})
        self.assertEqual(user.shared_temp(self.ENV, 'win32', home=self.home),
                         os.path.join(r'C:\lad', 'Temp'))

    def test_the_gate_fallback_log_base_sits_in_the_made_folder(self):
        from kitlib.gate import runner
        configured = os.path.join(self.home, 'not-yet', 'kit')
        self.write({'temp_dir': configured})
        real = user.shared_temp
        with mock.patch.object(user, 'shared_temp', lambda: real(self.ENV, 'win32', home=self.home)):
            base = runner.fallback_log_base('demo')
        self.assertEqual(os.path.dirname(base), configured)
        self.assertTrue(os.path.isdir(configured))


class SweepLogInSharedTemp(test_worktree_sweep.SweepCase):
    """`--sweep-merged --apply` with no log_dir writes its log into a configured temp_dir that
    did not exist yet (shared_temp makes it). shared_temp is patched to read this test's home:
    the home fence hides the machine config from a call with none."""

    def test_the_sweep_log_is_written_into_a_missing_temp_dir(self):
        khome = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, khome, True)
        os.makedirs(os.path.join(khome, user.DIR_NAME))
        configured = os.path.join(khome, 'not-yet', 'kit')
        with open(user.config_file(khome), 'w', encoding='utf-8') as f:
            json.dump({'temp_dir': configured}, f)
        self.landed('done')
        real = user.shared_temp
        with mock.patch.object(user, 'shared_temp', lambda: real(home=khome)):
            got = sweep.sweep(self.repo, apply=True, machine=test_worktree_sweep.free,
                              now=time.time() + test_worktree_sweep.LATER)
        out = '\n'.join(got.lines)
        self.assertNotIn('could not be written', out)
        logs = os.listdir(configured)
        self.assertEqual(len(logs), 1, out)
        with open(os.path.join(configured, logs[0]), encoding='utf-8') as f:
            self.assertIn('exit      ', f.read())


class Mkdtemp(HomeConfig):
    """user.mkdtemp: a small private folder in temp_parent (made if missing), else in this
    process's temp folder; never a config error."""

    def setUp(self):
        HomeConfig.setUp(self)
        self.proc = os.path.join(self.home, 'proc-temp')
        os.makedirs(self.proc)
        patch = mock.patch.object(tempfile, 'tempdir', self.proc)
        patch.start()
        self.addCleanup(patch.stop)

    def test_it_is_made_in_temp_dir_made_if_missing(self):
        configured = os.path.join(self.home, 'kit', 'shared')
        self.write({'temp_dir': configured})
        folder = user.mkdtemp('kit-x-', home=self.home)
        self.assertEqual(os.path.dirname(folder), configured)
        self.assertTrue(os.path.basename(folder).startswith('kit-x-'))
        self.assertTrue(os.path.isdir(folder))

    def test_no_temp_dir_gives_this_processs_temp(self):
        self.write({})
        folder = user.mkdtemp('kit-x-', home=self.home)
        self.assertEqual(os.path.dirname(folder), self.proc)

    def test_a_bad_config_gives_this_processs_temp(self):
        self.write({'temp_dir': 'relative'})
        self.assertEqual(os.path.dirname(user.mkdtemp('kit-x-', home=self.home)), self.proc)

    def test_a_temp_dir_that_cannot_be_made_gives_this_processs_temp(self):
        blocker = os.path.join(self.home, 'a-file')
        with open(blocker, 'w') as f:
            f.write('x')
        self.write({'temp_dir': os.path.join(blocker, 'below')})
        self.assertEqual(os.path.dirname(user.mkdtemp('kit-x-', home=self.home)), self.proc)

    def test_make_temps_last_resort_is_mkdtemp(self):
        got = os.path.join(self.home, 'kit-x-last')
        with mock.patch.object(user, 'work_temp', side_effect=RuntimeError('no')), \
                mock.patch.object(user, 'default_work_temp', side_effect=RuntimeError('no')), \
                mock.patch.object(user, 'mkdtemp', return_value=got) as made:
            folder, note = user.make_temp(self.home, 'kit-x-', home=self.home)
        made.assert_called_once_with('kit-x-', home=self.home)
        self.assertEqual(folder, got)
        self.assertIn('could not be used (no)', note)
        self.assertTrue(note.startswith('%s is in %s: ' % (got, self.home)), note)


def _calls(path):
    """(callee dotted name, keyword names) of every call in the module at `path`."""
    with open(path, encoding='utf-8') as f:
        tree = ast.parse(f.read(), path)
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and isinstance(node.func.value, ast.Name):
            out.append(('%s.%s' % (node.func.value.id, node.func.attr),
                        {k.arg for k in node.keywords}))
    return out


class PrivateIndexSites(unittest.TestCase):
    """The private-index folders of commit, the backlog store and pmdata init go in the shared
    temp (user.mkdtemp), never a bare tempfile.mkdtemp in the process's TEMP."""

    SITES = {'kitlib/commit/__init__.py': 1, 'kitlib/backlog/store.py': 2,
             'kitlib/pmdata/init.py': 1, 'kitlib/worktree/remove.py': 1}

    def test_each_site_calls_user_mkdtemp(self):
        for rel, count in self.SITES.items():
            calls = _calls(os.path.join(KIT, *rel.split('/')))
            bare = [c for c in calls if c[0] == 'tempfile.mkdtemp' and 'dir' not in c[1]]
            self.assertEqual(bare, [], rel)
            made = [c for c in calls if c[0].endswith('.mkdtemp') and c[0] != 'tempfile.mkdtemp']
            self.assertEqual(len(made), count, rel)


class ScratchRoots(HomeConfig):
    """scratch.roots lists the system temp, never temp_dir, and reads no machine config."""

    def test_the_system_temp_is_a_root_and_temp_dir_is_not(self):
        from kitlib.worktree import scratch
        lad = os.path.join(self.home, 'lad')
        os.makedirs(os.path.join(lad, 'Temp'))
        main = os.path.join(self.home, 'repo')
        os.makedirs(main)
        env = {'LOCALAPPDATA': lad, 'USERPROFILE': os.path.join(self.home, 'profile')}
        with mock.patch.object(user, 'temp_parent', return_value=main), \
                mock.patch.object(user, 'user_config', side_effect=AssertionError('config')):
            got, _dropped = scratch.roots(env, main)
        self.assertEqual([os.path.normcase(p) for p in got],
                         [os.path.normcase(os.path.join(lad, 'Temp'))])


if __name__ == '__main__':
    unittest.main()
