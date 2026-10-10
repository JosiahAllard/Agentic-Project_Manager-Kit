"""Mark roots across the machine config folder's move (docs/guard.md "Write jail"; docs/kit-json.md
"Moving the machine config folder"): no mark root may be on, over or in EITHER config folder.
While the config is still only in the retired folder, a root on the new folder would let a marked
session create the new config file, which kitlib.config.user.locate then reads instead of the old
one: its own guard settings. While the new file is read, the retired folder stays shut too."""
import json
import os

from kitlib.config import migration, user

from .test_jail import PM, Base


class MarkRootsBothFolders(Base):

    def setUp(self):
        Base.setUp(self)
        self.new_dir = os.path.join(self.w.home, user.DIR_NAME)
        self.old_dir = os.path.join(self.w.home, migration.OLD_DIR_NAME)

    def config(self, folder, roots):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, user.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump({'guard_mark_roots': {'pm': roots}}, f)

    def test_a_root_on_the_new_folder_cannot_take_over_an_old_only_config(self):
        for path in ('~/' + user.DIR_NAME, self.new_dir, '~/%s/sub' % user.DIR_NAME):
            with self.subTest(path):
                self.config(self.old_dir, [{'path': path, 'mode': 'open'}])
                self.assertEqual(user.locate(self.w.home).state, user.OLD)
                self.jailed(self.write(os.path.join(self.new_dir, user.FILE_NAME), env=PM, sub=False))
                self.jailed(self.write(os.path.join(self.new_dir, 'sub', 'x.json'), env=PM, sub=False))

    def test_a_root_on_the_retired_folder_stays_shut_once_the_new_file_is_read(self):
        for path in ('~/' + migration.OLD_DIR_NAME, self.old_dir):
            with self.subTest(path):
                self.config(self.new_dir, [{'path': path, 'mode': 'open'}])
                self.assertEqual(user.locate(self.w.home).state, user.NEW)
                self.jailed(self.write(os.path.join(self.old_dir, user.FILE_NAME), env=PM, sub=False))

    def test_a_sibling_of_either_folder_is_still_a_root(self):
        for name in (user.DIR_NAME, migration.OLD_DIR_NAME):
            with self.subTest(name):
                self.config(self.old_dir, [{'path': '~/%s-notes' % name, 'mode': 'open'}])
                self.allowed(self.write(os.path.join(self.w.home, name + '-notes', 'x.md'), env=PM, sub=False))
