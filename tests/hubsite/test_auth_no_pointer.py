"""kitlib.hubsite.auth.folder with no PM-repo pointer: a refusal that names the pointer step, never
a token folder in the home folder, so `hub.py setup` on a fresh machine writes nothing there. All
under a temp home and a temp PM repository; no token file of this machine is read."""
import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

from kitlib.config import user
from kitlib.config.types import ConfigError
from kitlib.hubsite import auth
from tests.home import PM_REPO_FENCE_ENV, pm_repo, temp_home

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_hub():
    spec = importlib.util.spec_from_file_location('hub_cli_no_pointer',
                                                  os.path.join(ROOT, 'tools', 'hub.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class NoPointerCase(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(PM_REPO_FENCE_ENV, None)      # no pointer, whatever the runner set
        user.forget()
        self.addCleanup(user.forget)

    def home_config_dir(self):
        return os.path.join(self.home, user.DIR_NAME)


class FolderTest(NoPointerCase):
    def test_no_pointer_is_refused_naming_the_pointer_step(self):
        self.assertEqual(user.locate(self.home).state, user.NONE)
        with self.assertRaises(ConfigError) as got:
            auth.folder(self.home)
        self.assertIn('--pm-repo', str(got.exception))
        self.assertIn('machine_setup.py', str(got.exception))
        self.assertFalse(os.path.exists(self.home_config_dir()))

    def test_the_default_home_is_refused_too(self):
        with self.assertRaises(ConfigError):
            auth.folder()

    def test_a_config_left_in_the_home_folder_is_refused_not_used(self):
        os.makedirs(self.home_config_dir())
        with open(os.path.join(self.home_config_dir(), user.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write('{}')
        self.assertEqual(user.locate(self.home).state, user.UNMOVED)
        with self.assertRaises(ConfigError) as got:
            auth.folder(self.home)
        self.assertIn('--pm-repo', str(got.exception))
        self.assertFalse(os.path.exists(os.path.join(self.home_config_dir(), auth.FOLDER_NAME)))

    def test_with_a_pointer_the_folder_is_in_the_pm_repository(self):
        pm = pm_repo(self)
        self.assertEqual(os.path.normcase(auth.folder(self.home)),
                         os.path.normcase(os.path.join(pm, user.DIR_NAME, auth.FOLDER_NAME)))
        self.assertFalse(os.path.exists(self.home_config_dir()))


class SetupTest(NoPointerCase):
    def test_hub_setup_refuses_and_writes_nothing_in_the_home_folder(self):
        hub = load_hub()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hub.main(['setup'])
        self.assertEqual(code, hub.client.USAGE, err.getvalue())
        self.assertIn('--pm-repo', err.getvalue())
        self.assertNotIn('token:', out.getvalue())
        self.assertFalse(os.path.exists(self.home_config_dir()))


if __name__ == '__main__':
    unittest.main()
