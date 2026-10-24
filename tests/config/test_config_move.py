"""The machine config's move into the PM repository (docs/kit-json.md "Moving the machine config
into the PM repo"). kitlib.config.user.home_location is the move's source: the home folder's file,
the new folder's (user.DIR_NAME) before the retired one's (kitlib.config.migration.OLD_DIR_NAME),
failing closed when the new one cannot be told. kitlib.config.user.locate is what the kit reads:
the PM repository's `.pm-kit` with a good PM-repo pointer, and a machine config still in the home
folder is an error (UNMOVED), never read and never "no config". Every case uses a scratch home
folder and the fenced pointer, never the user's."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from kitlib import backloglint, coordinatorstart, pmstart, pmwatch
from kitlib.config import ConfigError, migration, user
from kitlib.machinesetup import default_config_dirs
from tests.home import machine_config, pm_repo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'optional', 'agenttemp'))
import agent_temp_sweep as sweep                                   # noqa: E402

READER_KEYS = {'pm_start': {'hub_url': 'u'}, 'coordinator_start': {'k': 1}, 'pm_watch': {'w': 1},
               'backlog_lint': {'repos': {}}}


class _Home(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.new_dir = os.path.join(self.home, user.DIR_NAME)
        self.old_dir = os.path.join(self.home, migration.OLD_DIR_NAME)
        self.new = os.path.join(self.new_dir, user.FILE_NAME)
        self.old = os.path.join(self.old_dir, user.FILE_NAME)
        self.ledger = os.path.join(self.home, 'ledger', 'rulings.md')
        self.other_ledger = os.path.join(self.home, 'ledger', 'other.md')

    def pointed(self):
        """A PM repository the fenced pointer names; its `.pm-kit` folder is not made."""
        self.pm = pm_repo(self)
        self.pm_dir = os.path.join(self.pm, user.DIR_NAME)
        self.pm_file = os.path.join(self.pm_dir, user.FILE_NAME)
        return self.pm

    def put(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def read_bytes(self, path):
        with open(path, 'rb') as f:
            return f.read()


class HomeLocationTest(_Home):
    """The move's source: the home folder's one file, read as it is."""

    def read(self):
        return user.user_config(location=user.home_location(self.home))

    def test_the_names_differ_and_the_old_one_is_the_retired_folder(self):
        self.assertNotEqual(user.DIR_NAME, migration.OLD_DIR_NAME)
        self.assertFalse(user.DIR_NAME.startswith('.claude'))

    def test_neither_present_is_none_named_at_the_new_folder(self):
        loc = user.home_location(self.home)
        self.assertEqual((loc.state, loc.path, loc.error), (user.NONE, self.new, None))
        self.assertIsNone(self.read()['ledger_path'])

    def test_the_new_file_alone_is_read(self):
        self.put(self.new, {'ledger_path': self.ledger})
        self.assertEqual(user.home_location(self.home).state, user.NEW)
        self.assertEqual(self.read()['ledger_path'], self.ledger)

    def test_the_old_file_is_read_while_the_new_one_is_absent(self):
        self.put(self.old, {'ledger_path': self.ledger})
        loc = user.home_location(self.home)
        self.assertEqual((loc.state, loc.path, loc.error), (user.OLD, self.old, None))
        self.assertEqual(self.read()['ledger_path'], self.ledger)

    def test_a_new_folder_without_its_config_file_still_reads_the_old_file(self):
        # another writer (a state file, say) made the new folder first: the old file stays the source
        self.put(os.path.join(self.new_dir, 'some_state.json'), {})
        self.put(self.old, {'ledger_path': self.ledger})
        self.assertEqual(user.home_location(self.home).state, user.OLD)
        self.assertEqual(self.read()['ledger_path'], self.ledger)

    def test_both_present_reads_the_new_file_only(self):
        self.put(self.new, {'ledger_path': self.ledger})
        self.put(self.old, {'ledger_path': self.other_ledger, 'pending_path': self.other_ledger})
        loc = user.home_location(self.home)
        self.assertEqual((loc.state, loc.path), (user.BOTH, self.new))
        cfg = self.read()
        self.assertEqual(cfg['ledger_path'], self.ledger)
        self.assertIsNone(cfg['pending_path'])           # never merged from the old file

    def test_a_broken_new_file_fails_closed_and_never_falls_back(self):
        self.put(self.old, {'ledger_path': self.ledger})
        for body in ('{', '[]', json.dumps({'ledger_path': 'relative.md'}),
                     json.dumps({'a_key_this_kit_does_not_know': 1})):
            with self.subTest(body):
                self.put(self.new, body)
                with self.assertRaises(ConfigError) as cm:
                    self.read()
                self.assertIn(self.new, str(cm.exception))

    def test_a_new_folder_that_is_a_file_is_bad(self):
        self.put(self.new_dir, 'not a folder')
        self.put(self.old, {'ledger_path': self.ledger})
        loc = user.home_location(self.home)
        self.assertEqual((loc.state, loc.path), (user.BAD, self.new))
        self.assertIn(self.new_dir, loc.error)
        with self.assertRaises(ConfigError):
            self.read()

    def test_a_new_config_file_that_is_a_folder_is_bad(self):
        os.makedirs(self.new)
        self.put(self.old, {'ledger_path': self.ledger})
        self.assertEqual(user.home_location(self.home).state, user.BAD)

    def test_a_new_path_that_cannot_be_examined_is_bad_not_absent(self):
        self.put(self.old, {'ledger_path': self.ledger})
        real_stat = os.stat
        new_dir = os.path.normcase(self.new_dir)

        def stat(p, *a, **kw):
            if os.path.normcase(os.fspath(p)).startswith(new_dir):
                raise PermissionError(13, 'Access is denied', p)
            return real_stat(p, *a, **kw)
        with mock.patch.object(user.os, 'stat', stat), mock.patch.object(user.os, 'lstat', stat):
            loc = user.home_location(self.home)
        self.assertEqual(loc.state, user.BAD)
        self.assertIn('Access is denied', loc.error)

    def test_display_names_the_file_that_is_read(self):
        self.assertEqual(user.home_location(self.home).display(), '~/%s/%s' % (user.DIR_NAME, user.FILE_NAME))
        self.put(self.old, {})
        self.assertEqual(user.home_location(self.home).display(),
                         '~/%s/%s' % (migration.OLD_DIR_NAME, user.FILE_NAME))


class LocateTest(_Home):
    """What the kit reads: the PM repository's file, or nothing, or an error."""

    def test_no_pointer_and_no_home_file_is_an_empty_config_named_at_the_home_file(self):
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.error), (user.NONE, None))
        self.assertEqual(user.config_file(self.home), self.new)
        self.assertEqual(user.config_dir(self.home), self.new_dir)
        self.assertEqual(user.readable_file(self.home), self.new)
        self.assertIsNone(user.ledger_path(self.home))
        self.assertEqual(user.display(self.home), '~/%s/%s' % (user.DIR_NAME, user.FILE_NAME))

    def test_a_home_file_with_no_pointer_is_unmoved_never_read(self):
        for path in (self.new, self.old):
            with self.subTest(path):
                self.put(path, {'ledger_path': self.ledger})
                loc = user.locate(self.home)
                self.assertEqual((loc.state, loc.path, loc.leftover), (user.UNMOVED, path, path))
                self.assertIn('set the PM repo pointer', loc.error)
                self.assertIn('config_move.py', loc.error)
                with self.assertRaises(ConfigError):
                    user.ledger_path(self.home)
                with self.assertRaises(ConfigError):
                    user.readable_file(self.home)
                shutil.rmtree(os.path.dirname(path))

    def test_the_pm_repositorys_file_is_read(self):
        self.pointed()
        self.put(self.pm_file, {'ledger_path': self.ledger})
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.path, loc.folder, loc.leftover), (user.OK, self.pm_file, self.pm_dir, None))
        self.assertEqual(user.ledger_path(self.home), self.ledger)
        self.assertEqual(user.config_file(self.home), self.pm_file)
        self.assertEqual(user.config_dir(self.home), self.pm_dir)

    def test_a_pointer_with_no_pm_file_and_no_home_file_is_an_empty_config(self):
        self.pointed()
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.path, loc.error), (user.OK, self.pm_file, None))
        self.assertIsNone(user.ledger_path(self.home))

    def test_a_home_file_beside_the_pm_repositorys_is_a_leftover_and_an_error(self):
        self.pointed()
        self.put(self.pm_file, {'ledger_path': self.ledger})
        self.put(self.new, {'ledger_path': self.other_ledger, 'pending_path': self.other_ledger})
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.leftover), (user.LEFTOVER, self.new))
        self.assertIn(self.new, loc.error)
        with self.assertRaises(ConfigError):
            user.user_config(self.home)

    def test_a_home_file_and_no_pm_file_is_unmoved_naming_both(self):
        self.pointed()
        self.put(self.old, {'ledger_path': self.ledger})
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.leftover), (user.UNMOVED, self.old))
        self.assertIn(self.old, loc.error)
        self.assertIn(self.pm_file, loc.error)
        with self.assertRaises(ConfigError):
            user.user_config(self.home)

    def test_a_pm_folder_that_is_a_file_is_bad(self):
        self.pointed()
        self.put(self.pm_dir, 'not a folder')
        self.put(self.new, {'ledger_path': self.ledger})
        loc = user.locate(self.home)
        self.assertEqual(loc.state, user.BAD)
        self.assertIn(self.pm_dir, loc.error)
        self.assertEqual(user.config_file(self.home), self.pm_file)     # never raises
        with self.assertRaises(ConfigError):
            user.ledger_path(self.home)

    def test_a_pm_config_file_that_is_a_folder_is_bad(self):
        self.pointed()
        for path in (self.pm_file, os.path.join(self.pm_dir, user.LOCAL_FILE_NAME)):
            with self.subTest(path):
                shutil.rmtree(self.pm_dir, True)
                os.makedirs(path)
                self.assertEqual(user.locate(self.home).state, user.BAD)
                with self.assertRaises(ConfigError):
                    user.user_config(self.home)

    def test_a_pm_folder_that_cannot_be_examined_is_bad_not_absent(self):
        self.pointed()
        self.put(self.new, {'ledger_path': self.ledger})
        real_stat = os.stat
        pm_dir = os.path.normcase(self.pm_dir)

        def stat(p, *a, **kw):
            if os.path.normcase(os.fspath(p)).startswith(pm_dir):
                raise PermissionError(13, 'Access is denied', p)
            return real_stat(p, *a, **kw)
        with mock.patch.object(user.os, 'stat', stat), mock.patch.object(user.os, 'lstat', stat):
            loc = user.locate(self.home)
            self.assertEqual(loc.state, user.BAD)
            self.assertIn('Access is denied', loc.error)
            with self.assertRaises(ConfigError):
                user.ledger_path(self.home)


class WritableTest(_Home):

    def test_no_pointer_refuses_naming_the_pointer_step_for_every_key(self):
        for key in (None, 'owner_email', 'agent_temp_sweep'):
            with self.assertRaises(ConfigError, msg=key) as cm:
                user.writable_file(self.home, key)
            self.assertIn('no PM repo pointer is set', str(cm.exception))
            self.assertIn('tools/machine_setup.py --pm-repo', str(cm.exception))
        self.assertFalse(os.path.exists(self.new))

    def test_a_home_file_refuses_with_the_move(self):
        for path in (self.new, self.old):
            with self.subTest(path):
                self.put(path, {'ledger_path': self.ledger})
                with self.assertRaises(ConfigError) as cm:
                    user.writable_file(self.home)
                self.assertIn(path, str(cm.exception))
                os.remove(path)

    def test_a_bad_pm_folder_refuses(self):
        self.pointed()
        self.put(self.pm_dir, 'not a folder')
        with self.assertRaises(ConfigError):
            user.writable_file(self.home)


class ReadersTest(_Home):
    """The modules that read their own key straight from the file follow the same choice."""

    def env(self):
        return mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})

    def test_each_reader_reads_the_pm_repositorys_file(self):
        self.pointed()
        machine_config(self.home, dict(READER_KEYS, machine_lock={'notice': {'dir': self.home}}), case=self)
        with self.env():
            self.assertEqual(pmstart.load_config(), {'hub_url': 'u'})
            self.assertEqual(coordinatorstart.load_config(), {'k': 1})
            self.assertEqual(pmwatch.load_config(), {'w': 1})
            self.assertEqual(pmwatch.configured_notice_dir(), self.home)
            self.assertEqual(backloglint.load_config(), {})

    def test_each_reader_takes_a_home_file_or_a_bad_location_for_a_config_error_never_no_config(self):
        # the home file holds every key, so a reader that read it or read "absent" would answer
        body = dict(READER_KEYS, machine_lock={'notice': {'dir': self.home}},
                    agent_temp_sweep={'dir': self.home})
        self.put(self.old, body)
        cases = (('home file, no pointer', self.old_dir), ('home file, empty PM repository', self.old_dir),
                 ('PM folder is a file', None), ('PM config is a folder', None))
        for bad, named in cases:
            with self.subTest(bad):
                if bad != 'home file, no pointer':
                    self.pointed()
                    if bad == 'PM folder is a file':
                        self.put(self.pm_dir, 'not a folder')
                    elif bad == 'PM config is a folder':
                        os.makedirs(self.pm_file)
                    named = named or self.pm_dir
                self.assertIn(user.locate(self.home).state, (user.UNMOVED, user.BAD))
                with self.env():
                    for name, read, error in (
                            ('pmstart', pmstart.load_config, ConfigError),
                            ('coordinatorstart', coordinatorstart.load_config, ConfigError),
                            ('pmwatch', pmwatch.load_config, ConfigError),
                            ('pmwatch notice dir', pmwatch.configured_notice_dir, ConfigError),
                            ('backloglint', backloglint.load_config, backloglint.LintConfigError),
                            ('agent_temp_sweep', sweep.load, sweep.SweepConfigError)):
                        with self.subTest(name):
                            with self.assertRaises(error) as cm:
                                read()
                            self.assertIn(named, str(cm.exception))

    def test_the_coordinator_start_says_it_could_not_check_the_config(self):
        self.pointed()
        self.put(self.pm_dir, 'not a folder')
        with self.env():
            text = coordinatorstart.render(env={coordinatorstart.ROLE_VAR: coordinatorstart.ROLE})
        self.assertIn('could not check config: ConfigError', text)


class BadLocationCliTest(_Home):
    """The entry points over those readers say the machine config cannot be read and exit 2."""

    def run_main(self, main, argv):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(argv)
        return code, err.getvalue()

    def test_each_says_machine_config_and_exits_2(self):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(root, 'tools')
        if d not in sys.path:
            sys.path.append(d)
        import pm_watch
        self.pointed()
        self.put(self.pm_dir, 'not a folder')
        self.put(self.new, {'pm_start': {'sessions_file': 's.json'},
                            'machine_lock': {'notice': {'dir': self.home}}})
        for name, main in (('pm_watch', pm_watch.main),):
            with self.subTest(name):
                code, err = self.run_main(main, [])
                self.assertEqual(code, 2, err)
                self.assertIn('machine config', err)
                self.assertIn('not a folder', err)


class MachineSetupTest(_Home):

    def test_neither_config_folder_is_taken_for_a_claude_account_folder(self):
        for name in ('.claude', '.claude-two', user.DIR_NAME, migration.OLD_DIR_NAME):
            os.makedirs(os.path.join(self.home, name))
        self.assertEqual([os.path.basename(d) for d in default_config_dirs(self.home)],
                         ['.claude', '.claude-two'])


if __name__ == '__main__':
    unittest.main()
