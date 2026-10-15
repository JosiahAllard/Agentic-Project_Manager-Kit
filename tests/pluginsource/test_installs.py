"""Where each account folder loads the kit plugin from: kitlib.pluginsource.claude installed_path
and installs, read from the account's install record (plugins/installed_plugins.json). Every
folder is a temp tree; nothing is checked on disk but the record."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib import pluginsource
from kitlib.pluginsource import claude


class InstallsCase(unittest.TestCase):
    def setUp(self):
        self.top = tempfile.mkdtemp(prefix='kit-installs-')
        self.addCleanup(shutil.rmtree, self.top, True)

    def account(self, name, records=None, text=None):
        """An account folder whose install record lists `records` for the kit (None: no record
        file; `text`: the file's raw text instead)."""
        folder = os.path.join(self.top, name)
        os.makedirs(os.path.join(folder, 'plugins'))
        if text is not None or records is not None:
            with open(os.path.join(folder, *claude.INSTALLED), 'w', encoding='utf-8') as f:
                f.write(text if text is not None else json.dumps(
                    {'version': 2, 'plugins': {pluginsource.PLUGIN_ID: records}}))
        return folder

    def path(self, *parts):
        return os.path.join(self.top, *parts)


class InstalledPathTest(InstallsCase):
    def test_the_user_scope_install_is_preferred_over_an_earlier_record(self):
        acct = self.account('a', [
            {'scope': 'project', 'installPath': self.path('project-copy')},
            {'scope': 'user', 'installPath': self.path('user-copy')}])
        self.assertEqual(claude.installed_path(acct), self.path('user-copy'))

    def test_with_no_user_scope_install_the_first_absolute_path_is_taken(self):
        acct = self.account('a', [
            {'scope': 'local', 'installPath': 'relative'},
            {'scope': 'project', 'installPath': self.path('first')},
            {'scope': 'local', 'installPath': self.path('second')}])
        self.assertEqual(claude.installed_path(acct), self.path('first'))

    def test_a_missing_folder_is_still_named_the_record_is_all_that_is_read(self):
        acct = self.account('a', [{'scope': 'user', 'installPath': self.path('gone')}])
        self.assertFalse(os.path.exists(self.path('gone')))
        self.assertEqual(claude.installed_path(acct), self.path('gone'))

    def test_no_record_a_bad_record_or_no_kit_install_is_none(self):
        cases = (self.account('none'), self.account('bad', text='{not json'),
                 self.account('list', text='[]'),
                 self.account('other', text=json.dumps({'plugins': {'x@y': []}})),
                 self.account('rel', [{'scope': 'user', 'installPath': 'relative'}]),
                 self.account('odd', ['not a record']))
        for acct in cases:
            self.assertIsNone(claude.installed_path(acct), acct)


class InstallsTest(InstallsCase):
    def test_each_account_with_an_install_is_listed_with_its_folder_in_order(self):
        one = self.account('one', [{'scope': 'user', 'installPath': self.path('c1')}])
        none = self.account('none')
        two = self.account('two', [{'scope': 'user', 'installPath': self.path('c2')}])
        want = [(self.path('c1'), one), (self.path('c2'), two)]
        self.assertEqual(claude.installs([one, none, two]), want)
        self.assertEqual(pluginsource.installs([one, none, two]), want)


if __name__ == '__main__':
    unittest.main()
