"""preflight's `config` line (kitlib.preflight.configline; docs/kit-json.md "Moving the machine
config folder"): which machine config file is read, and a machine that has both folders. It
names keys, never values. Every case uses a scratch home folder."""
import json
import os
import shutil
import tempfile
import unittest

from kitlib.config import migration, user
from kitlib.preflight import configline

SECRET = 'value-never-printed'


class ConfigLineTest(unittest.TestCase):

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)
        self.new_name = '~/%s/%s' % (user.DIR_NAME, user.FILE_NAME)
        self.old_name = '~/%s/%s' % (migration.OLD_DIR_NAME, user.FILE_NAME)

    def put(self, name, data):
        folder = os.path.join(self.home, name)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, user.FILE_NAME), 'w', encoding='utf-8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def line(self):
        out = configline.line(self.home)
        self.assertTrue(out.startswith('config    '), out)
        self.assertNotIn(SECRET, out)
        return out

    def test_none(self):
        out = self.line()
        self.assertIn('none', out)
        self.assertIn(self.new_name, out)

    def test_the_new_file(self):
        self.put(user.DIR_NAME, {'pending_path': os.path.join(self.home, SECRET)})
        self.assertEqual(self.line(), 'config    %s' % self.new_name)

    def test_the_old_file_is_a_fallback_to_move(self):
        self.put(migration.OLD_DIR_NAME, {})
        out = self.line()
        self.assertIn('FALLBACK', out)
        self.assertIn(self.old_name, out)
        self.assertIn(self.new_name, out)

    def test_both_with_the_same_content(self):
        body = json.dumps({'pending_path': os.path.join(self.home, SECRET)})
        self.put(user.DIR_NAME, body)
        self.put(migration.OLD_DIR_NAME, body)
        out = self.line()
        self.assertIn('WARN both', out)
        self.assertIn('same content', out)

    def test_both_differing_names_the_keys_only(self):
        self.put(user.DIR_NAME, {'pending_path': os.path.join(self.home, SECRET), 'ledger_repos': ['a']})
        self.put(migration.OLD_DIR_NAME, {'pending_path': os.path.join(self.home, 'x'), 'ledger_repos': ['a'],
                                          'temp_dir': os.path.join(self.home, SECRET)})
        out = self.line()
        self.assertIn('WARN both', out)
        self.assertIn('DIFFERENT (keys: pending_path, temp_dir)', out)

    def test_a_broken_new_file(self):
        self.put(user.DIR_NAME, '{')
        self.put(migration.OLD_DIR_NAME, {})
        out = self.line()
        self.assertIn('BROKEN', out)

    def test_a_new_folder_that_is_a_file(self):
        with open(os.path.join(self.home, user.DIR_NAME), 'w', encoding='utf-8') as f:
            f.write('x')
        self.assertIn('BROKEN', self.line())


if __name__ == '__main__':
    unittest.main()
