"""Mark roots across the machine config folder's move (docs/guard.md "Write jail"; docs/kit-json.md
"Moving the machine config folder"): no mark root may be on, over or in EITHER config folder.
While the config is still only in the retired folder, a root on the new folder would let a marked
session create the new config file, which kitlib.config.user.locate then reads instead of the old
one: its own guard settings. While the new file is read, the retired folder stays shut too. The
same holds for a root of every other kind: the session's own root, a data root and a temp root."""
import json
import os

from kitlib.config import migration, user
from kitlib.guard import decide

from .test_jail import PM, Base
from .world import event


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


class OwnRootNotAConfigFolder(Base):
    """A session run outside git in either config folder (or in one inside or over it) has no
    own root there: it cannot write config.json, the file that names its guard settings and the
    ledger the guard protects. The old-only machine is the case that matters most: a new file
    written there would replace the old one for every reader."""

    def setUp(self):
        Base.setUp(self)
        self.new_dir = os.path.join(self.w.home, user.DIR_NAME)
        self.old_dir = os.path.join(self.w.home, migration.OLD_DIR_NAME)
        for d in (self.new_dir, self.old_dir):
            os.makedirs(os.path.join(d, 'sub'))
        with open(os.path.join(self.old_dir, user.FILE_NAME), 'w', encoding='utf-8') as f:
            json.dump({'ledger_path': os.path.join(self.w.base, 'ledger.md')}, f)
        self.assertEqual(user.locate(self.w.home).state, user.OLD)

    def each_session(self):
        return (('marked', dict(env=PM, sub=False)), ('subagent', dict(sub=True)))

    def test_a_session_in_the_new_folder_cannot_write_its_config_file(self):
        for cwd in (self.new_dir, os.path.join(self.new_dir, 'sub')):
            for who, kw in self.each_session():
                with self.subTest(cwd=cwd, who=who):
                    self.jailed(self.write(os.path.join(self.new_dir, user.FILE_NAME), cwd=cwd, **kw))
                    self.assertFalse(os.path.exists(os.path.join(self.new_dir, user.FILE_NAME)))

    def test_a_session_in_the_old_folder_cannot_write_its_config_file(self):
        for cwd in (self.old_dir, os.path.join(self.old_dir, 'sub')):
            for who, kw in self.each_session():
                with self.subTest(cwd=cwd, who=who):
                    self.jailed(self.write(os.path.join(self.old_dir, user.FILE_NAME), cwd=cwd, **kw))

    def test_a_session_over_the_home_folder_has_no_own_root(self):
        # base holds home, so a root there would hold both config folders
        for who, kw in self.each_session():
            with self.subTest(who):
                self.jailed(self.write(os.path.join(self.new_dir, user.FILE_NAME), cwd=self.w.base, **kw))

    def test_a_plain_folder_beside_them_is_still_its_own_root(self):
        notes = os.path.join(self.w.home, user.DIR_NAME + '-notes')
        os.makedirs(notes)
        self.allowed(self.write(os.path.join(notes, 'x.md'), cwd=notes, env=PM, sub=False))


class NoRootOfAnyKindOnAConfigFolder(Base):
    """A data root or a temp root on, over or inside either config folder is dropped as an own or
    mark root is (jail.roots): a .kit.json guard.data_roots entry, or TMP, AGENT_TEMP or the system
    temp folder, at the home folder or above would otherwise let a jailed session write
    config.json. A read-only data root there is kept: it only denies."""

    def setUp(self):
        Base.setUp(self)
        self.new_dir = os.path.join(self.w.home, user.DIR_NAME)
        self.old_dir = os.path.join(self.w.home, migration.OLD_DIR_NAME)
        for d in (self.new_dir, self.old_dir):
            os.makedirs(os.path.join(d, 'sub'))

    def targets(self):
        return [os.path.join(d, user.FILE_NAME) for d in (self.new_dir, self.old_dir)]

    def data_root(self, path, mode):
        self.w.kit({'repo': {'name': 'demo', 'push_block': True},
                    'guard': {'data_roots': [{'path': path, 'mode': mode}]}})

    def each_session(self, env=None):
        env = env or {}
        return (('marked', dict(env=dict(PM, **env), sub=False)), ('subagent', dict(env=env, sub=True)))

    def test_a_data_root_on_or_over_the_home_folder_cannot_write_a_config_file(self):
        for path in (self.w.home, self.w.base, self.new_dir, os.path.join(self.old_dir, 'sub')):
            for mode in ('open', 'add-only'):
                self.data_root(path, mode)
                for who, kw in self.each_session():
                    for target in self.targets():
                        with self.subTest(path=path, mode=mode, who=who, target=target):
                            self.jailed(self.write(target, **kw))

    def test_a_temp_root_on_or_over_the_home_folder_cannot_write_a_config_file(self):
        for var in ('TMP', 'AGENT_TEMP'):
            for path in (self.w.home, self.w.base, self.new_dir):
                for who, kw in self.each_session({var: path}):
                    for target in self.targets():
                        with self.subTest(var=var, path=path, who=who, target=target):
                            self.jailed(self.write(target, **kw))

    def test_a_system_temp_folder_over_the_home_folder_cannot_write_a_config_file(self):
        for temp in (self.w.home, self.w.base):
            for target in self.targets():
                with self.subTest(temp=temp, target=target):
                    ev = event('Write', {'file_path': target, 'content': 'x'}, self.w.wt, True)
                    self.jailed(decide(ev, self.w.ctx(environ={}, tempdir=temp)))

    def test_a_data_or_temp_root_beside_the_config_folders_still_works(self):
        notes = os.path.join(self.w.home, user.DIR_NAME + '-notes')
        agents = os.path.join(self.w.home, 'agenttemp')
        for d in (notes, agents):
            os.makedirs(d)
        self.data_root(notes, 'open')
        for who, kw in self.each_session({'AGENT_TEMP': agents}):
            with self.subTest(who):
                self.allowed(self.write(os.path.join(notes, 'x.md'), **kw))
                self.allowed(self.write(os.path.join(agents, 'x.txt'), **kw))

    def test_a_read_only_data_root_over_the_home_folder_still_denies(self):
        agents = os.path.join(self.w.home, 'agenttemp')
        os.makedirs(agents)
        self.data_root(self.w.home, 'read-only')
        for who, kw in self.each_session({'AGENT_TEMP': agents}):
            with self.subTest(who):
                d = self.jailed(self.write(os.path.join(agents, 'x.txt'), **kw))
                self.assertIn('read-only', d.reason)
                self.jailed(self.write(self.targets()[0], **kw))
