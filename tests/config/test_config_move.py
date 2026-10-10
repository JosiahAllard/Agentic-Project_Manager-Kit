"""The machine config folder's move (docs/kit-json.md "Moving the machine config folder"):
kitlib.config.user.locate reads the new folder (user.DIR_NAME), falls back to the old one
(kitlib.config.migration.OLD_DIR_NAME) only while the new config file is absent, fails closed
when the new one cannot be told, and writers write the new folder only. Every case uses a
scratch home folder, never the user's."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from kitlib import backloglint, coordinatorstart, pmstart, pmwatch
from kitlib.config import ConfigError, migration, user
from kitlib.machinesetup import default_config_dirs
from kitlib.pmdata import init as pminit


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

    def put(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(body if isinstance(body, str) else json.dumps(body))

    def read_bytes(self, path):
        with open(path, 'rb') as f:
            return f.read()


class LocateTest(_Home):

    def test_the_names_differ_and_the_old_one_is_the_retired_folder(self):
        self.assertNotEqual(user.DIR_NAME, migration.OLD_DIR_NAME)
        self.assertFalse(user.DIR_NAME.startswith('.claude'))

    def test_neither_present_is_an_empty_config_named_at_the_new_folder(self):
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.error), (user.NONE, None))
        self.assertEqual(user.config_file(self.home), self.new)
        self.assertEqual(user.config_dir(self.home), self.new_dir)
        self.assertIsNone(user.ledger_path(self.home))

    def test_the_new_file_alone_is_read(self):
        self.put(self.new, {'ledger_path': self.ledger})
        self.assertEqual(user.locate(self.home).state, user.NEW)
        self.assertEqual(user.ledger_path(self.home), self.ledger)
        self.assertEqual(user.config_file(self.home), self.new)

    def test_the_old_file_is_read_while_the_new_one_is_absent(self):
        self.put(self.old, {'ledger_path': self.ledger})
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.path, loc.error), (user.OLD, self.old, None))
        self.assertEqual(user.ledger_path(self.home), self.ledger)
        self.assertEqual(user.config_dir(self.home), self.old_dir)

    def test_a_new_folder_without_its_config_file_still_reads_the_old_file(self):
        # another writer (hub state, say) made the new folder first: the ledger stays known
        os.makedirs(self.new_dir)
        self.put(os.path.join(self.new_dir, 'hub_backlog_state.json'), {})
        self.put(self.old, {'ledger_path': self.ledger})
        self.assertEqual(user.locate(self.home).state, user.OLD)
        self.assertEqual(user.ledger_path(self.home), self.ledger)

    def test_both_present_reads_the_new_file_only(self):
        self.put(self.new, {'ledger_path': self.ledger})
        self.put(self.old, {'ledger_path': self.other_ledger, 'pending_path': self.other_ledger})
        loc = user.locate(self.home)
        self.assertEqual((loc.state, loc.path), (user.BOTH, self.new))
        cfg = user.user_config(self.home)
        self.assertEqual(cfg['ledger_path'], self.ledger)
        self.assertIsNone(cfg['pending_path'])           # never merged from the old file

    def test_a_broken_new_file_fails_closed_and_never_falls_back(self):
        self.put(self.old, {'ledger_path': self.ledger})
        for body in ('{', '[]', json.dumps({'ledger_path': 'relative.md'}),
                     json.dumps({'a_key_this_kit_does_not_know': 1})):
            with self.subTest(body):
                self.put(self.new, body)
                with self.assertRaises(ConfigError) as cm:
                    user.user_config(self.home)
                self.assertIn(self.new, str(cm.exception))

    def test_a_new_folder_that_is_a_file_is_bad(self):
        self.put(self.new_dir, 'not a folder')
        self.put(self.old, {'ledger_path': self.ledger})
        loc = user.locate(self.home)
        self.assertEqual(loc.state, user.BAD)
        self.assertIn(self.new_dir, loc.error)
        self.assertEqual(user.config_file(self.home), self.new)     # never raises
        with self.assertRaises(ConfigError):
            user.ledger_path(self.home)

    def test_a_new_config_file_that_is_a_folder_is_bad(self):
        os.makedirs(self.new)
        self.put(self.old, {'ledger_path': self.ledger})
        self.assertEqual(user.locate(self.home).state, user.BAD)
        with self.assertRaises(ConfigError):
            user.user_config(self.home)

    def test_a_new_path_that_cannot_be_examined_is_bad_not_absent(self):
        self.put(self.old, {'ledger_path': self.ledger})
        real_stat = os.stat
        new_dir = os.path.normcase(self.new_dir)

        def stat(p, *a, **kw):
            if os.path.normcase(os.fspath(p)).startswith(new_dir):
                raise PermissionError(13, 'Access is denied', p)
            return real_stat(p, *a, **kw)
        with mock.patch.object(user.os, 'stat', stat), mock.patch.object(user.os, 'lstat', stat):
            loc = user.locate(self.home)
            self.assertEqual(loc.state, user.BAD)
            self.assertIn('Access is denied', loc.error)
            with self.assertRaises(ConfigError):
                user.ledger_path(self.home)

    def test_display_names_the_file_that_is_read(self):
        self.assertEqual(user.display(self.home), '~/%s/%s' % (user.DIR_NAME, user.FILE_NAME))
        self.put(self.old, {})
        self.assertEqual(user.display(self.home), '~/%s/%s' % (migration.OLD_DIR_NAME, user.FILE_NAME))


class WritableTest(_Home):

    def test_neither_present_writes_the_new_folder(self):
        self.assertEqual(user.writable_file(self.home), self.new)

    def test_the_new_file_or_both_write_the_new_folder(self):
        self.put(self.new, {})
        self.assertEqual(user.writable_file(self.home), self.new)
        self.put(self.old, {})
        self.assertEqual(user.writable_file(self.home), self.new)

    def test_the_old_file_alone_refuses_with_the_move(self):
        self.put(self.old, {'ledger_path': self.ledger})
        with self.assertRaises(ConfigError) as cm:
            user.writable_file(self.home)
        self.assertIn(self.old, str(cm.exception))
        self.assertIn(self.new, str(cm.exception))

    def test_a_bad_new_folder_refuses(self):
        self.put(self.new_dir, 'not a folder')
        with self.assertRaises(ConfigError):
            user.writable_file(self.home)

    def test_write_machine_writes_the_new_folder_when_there_is_none(self):
        pminit.write_machine(self.home, {'path': os.path.join(self.home, 'data')})
        with open(self.new, encoding='utf-8') as f:
            self.assertEqual(json.load(f)['pm_data']['path'], os.path.join(self.home, 'data'))
        self.assertFalse(os.path.exists(self.old_dir))

    def test_write_machine_refuses_an_old_only_machine_and_writes_nothing(self):
        self.put(self.old, {'ledger_path': self.ledger})
        before = self.read_bytes(self.old)
        with self.assertRaises(ConfigError):
            pminit.write_machine(self.home, {'path': os.path.join(self.home, 'data')})
        self.assertFalse(os.path.exists(self.new))
        self.assertEqual(self.read_bytes(self.old), before)

    def test_write_machine_with_both_keeps_the_new_keys_and_leaves_the_old_file(self):
        self.put(self.new, {'ledger_path': self.ledger})
        self.put(self.old, {'ledger_path': self.other_ledger})
        before = self.read_bytes(self.old)
        pminit.write_machine(self.home, {'path': os.path.join(self.home, 'data')})
        with open(self.new, encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data['ledger_path'], self.ledger)
        self.assertIn('pm_data', data)
        self.assertEqual(self.read_bytes(self.old), before)


class ReadersTest(_Home):
    """The modules that read their own key straight from the file follow the same choice."""

    def env(self):
        return mock.patch.dict(os.environ, {'USERPROFILE': self.home, 'HOME': self.home})

    def test_each_reader_falls_back_to_the_old_file(self):
        self.put(self.old, {'pm_start': {'hub_url': 'u'}, 'coordinator_start': {'k': 1},
                            'pm_watch': {'w': 1}, 'machine_lock': {'notice': {'dir': self.home}},
                            'backlog_lint': {'repos': {}}})
        with self.env():
            self.assertEqual(pmstart.load_config(), {'hub_url': 'u'})
            self.assertEqual(coordinatorstart.load_config(), {'k': 1})
            self.assertEqual(pmwatch.load_config(), {'w': 1})
            self.assertEqual(pmwatch.configured_notice_dir(), self.home)
            self.assertEqual(backloglint.load_config(), {})

    def test_each_reader_prefers_the_new_file(self):
        self.put(self.old, {'pm_start': {'hub_url': 'old'}})
        self.put(self.new, {'pm_start': {'hub_url': 'new'}})
        with self.env():
            self.assertEqual(pmstart.load_config(), {'hub_url': 'new'})


class MachineSetupTest(_Home):

    def test_neither_config_folder_is_taken_for_a_claude_account_folder(self):
        for name in ('.claude', '.claude-two', user.DIR_NAME, migration.OLD_DIR_NAME):
            os.makedirs(os.path.join(self.home, name))
        self.assertEqual([os.path.basename(d) for d in default_config_dirs(self.home)],
                         ['.claude', '.claude-two'])


if __name__ == '__main__':
    unittest.main()
