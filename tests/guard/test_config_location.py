"""The guard's machine config location (kitlib.guard.context Ctx.config_location and user): the
PM-repo pointer checked with the Ctx's own classify and the branch from the work tree's HEAD file
(kitlib.guard.place.head_branch), so a hook call starts no git for it; the PM repository's two
files read tolerantly, each key from its own file; the pointer argument and the location kept
apart; the hub's token folders (kitlib.guard.rules.hub_secret_folders); and refusal_log's location
read with no git child. Every home and PM repository is a temp folder under the fence
(tests.home)."""
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.guard import context, place, refusal_log, rules
from tests import gitfixture
from tests.home import pm_repo, temp_home


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(body if isinstance(body, str) else json.dumps(body))


class _Case(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.pm = pm_repo(self)
        self.folder = os.path.join(self.pm, kuser.DIR_NAME)
        self.policy = os.path.join(self.folder, kuser.FILE_NAME)
        self.local = os.path.join(self.folder, kuser.LOCAL_FILE_NAME)
        self.kit = tempfile.mkdtemp(prefix='kit-guard-kit-')
        self.addCleanup(gitfixture.rmtree, self.kit)

    def ctx(self, **kw):
        return context.Ctx(environ=dict(os.environ), home=self.home, kit=self.kit, **kw)

    def no_git(self):
        """A patch that fails the test on any child process (git included)."""
        return mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('a child process was started'))


class HeadBranchTest(unittest.TestCase):
    def setUp(self):
        self.git_dir = tempfile.mkdtemp(prefix='kit-head-')
        self.addCleanup(gitfixture.rmtree, self.git_dir)

    def head(self, text):
        write(os.path.join(self.git_dir, 'HEAD'), text)
        return place.head_branch(self.git_dir)

    def test_a_branch_is_named(self):
        self.assertEqual(self.head('ref: refs/heads/main\n'), ('main', None))
        self.assertEqual(self.head('ref: refs/heads/kit/a-b'), ('kit/a-b', None))

    def test_a_commit_is_a_detached_head(self):
        self.assertEqual(self.head('a' * 40 + '\n'), ('', None))
        self.assertEqual(self.head('b' * 64), ('', None))

    def test_anything_else_is_left_to_git(self):
        for text in ('ref: refs/remotes/origin/main', 'ref: refs/heads/', 'A' * 40, 'a' * 39, ''):
            self.assertIsNone(self.head(text), repr(text))
        os.remove(os.path.join(self.git_dir, 'HEAD'))
        self.assertIsNone(place.head_branch(self.git_dir))


class LocationTest(_Case):
    def test_a_good_pointer_is_checked_with_no_git_child(self):
        with self.no_git():
            loc = self.ctx().config_location()
        self.assertEqual((loc.state, loc.pm), (kuser.OK, os.path.normpath(self.pm)))
        self.assertEqual(loc.pm_data['branch'], 'main')

    def test_the_pointer_check_reuses_the_ctx_classify(self):
        seen = []

        def classify(path):
            seen.append(os.path.normcase(os.path.normpath(path)))
            return place.classify(path)
        c = self.ctx(classify=classify)
        c.config_location()
        c.place(self.pm)
        c.config_location()
        self.assertEqual(seen.count(os.path.normcase(os.path.normpath(self.pm))), 1)

    def test_a_head_file_that_says_nothing_asks_git(self):
        asked = []

        class Git(object):
            def current_branch(self, cwd):
                asked.append(cwd)
                return 'main', None
        # the git directory classify names holds a HEAD that names nothing
        odd = tempfile.mkdtemp(prefix='kit-odd-git-')
        self.addCleanup(gitfixture.rmtree, odd)
        write(os.path.join(odd, 'HEAD'), 'not a head\n')

        def classify(path):
            return place.classify(path)._replace(git_dir=odd)
        loc = self.ctx(git=Git(), classify=classify).config_location()
        self.assertEqual(loc.state, kuser.OK)
        self.assertEqual(len(asked), 1)

    def test_one_location_per_ctx(self):
        c = self.ctx()
        self.assertIs(c.config_location(), c.config_location())

    def test_the_pointer_argument_does_not_move_the_machine_config(self):
        elsewhere = os.path.join(self.home, 'elsewhere')
        c = self.ctx(pointer=lambda: elsewhere)
        self.assertEqual(c.pm_repo_pointer(), elsewhere)
        self.assertEqual(c.config_location().pm, os.path.normpath(self.pm))

    def test_the_location_argument_replaces_the_read(self):
        given = kuser.Location(kuser.NONE, 'Q:\\c.json', None, None, None, None, None, None, self.home, 'Q:\\c.json')
        with mock.patch.object(kuser, 'locate', side_effect=AssertionError('located')):
            self.assertIs(self.ctx(location=lambda: given).config_location(), given)


class TolerantReadTest(_Case):
    def test_each_key_comes_from_its_own_file(self):
        write(self.policy, {'ledger_path': 'Q:\\ledger.md', 'owner_email': 'misplaced@example.invalid'})
        write(self.local, {'owner_email': 'owner@example.invalid', 'guard_mcp_prefix': 'mcp__x'})
        u = self.ctx().user()
        self.assertEqual((u['ledger_path'], u['owner_email']), ('Q:\\ledger.md', 'owner@example.invalid'))
        self.assertIsNone(u['guard_mcp_prefix'])
        self.assertNotIn('broken', u)
        self.assertTrue(u['error'])                 # the strict reader refuses both misplaced keys

    def test_a_relative_ledger_path_is_read_against_the_pm_repository(self):
        write(self.policy, {'ledger_path': 'rulings\\ledger.md'})
        u = self.ctx().user()
        self.assertEqual(u['ledger_path'], os.path.join(os.path.normpath(self.pm), 'rulings', 'ledger.md'))
        self.assertNotIn('broken', u)

    def test_pm_data_is_the_locations(self):
        write(self.policy, {})
        u = self.ctx().user()
        self.assertEqual(u['pm_data'], self.ctx().config_location().pm_data)
        self.assertEqual(u['pm_data']['path'], os.path.normpath(self.pm))

    def test_a_broken_local_file_is_broken_naming_itself(self):
        write(self.policy, {'ledger_path': 'Q:\\ledger.md'})
        write(self.local, '{')
        u = self.ctx().user()
        self.assertIn('broken', u)
        self.assertIn(kuser.LOCAL_FILE_NAME, u['broken'])

    def test_an_unmoved_config_reads_its_leftover_and_is_broken(self):
        leftover = os.path.join(self.home, kuser.DIR_NAME, kuser.FILE_NAME)
        write(leftover, {'ledger_path': 'Q:\\old-ledger.md'})
        c = self.ctx()
        self.assertEqual(c.config_location().state, kuser.UNMOVED)
        u = c.user()
        self.assertEqual(u['ledger_path'], 'Q:\\old-ledger.md')
        self.assertEqual(u['broken'], c.config_location().error)


class LeftoverTest(_Case):
    """A home config.json beside the PM repository's files (LEFTOVER): still broken, so every
    subagent write is denied, and the PM repository's ledger keys are read, so a main session's
    edit of the ledger is refused too; nothing else of the file is read."""

    def setUp(self):
        super().setUp()
        self.books = tempfile.mkdtemp(prefix='kit-ledger-')
        self.addCleanup(gitfixture.rmtree, self.books)
        self.ledger = os.path.join(self.books, 'LEDGER.md')
        self.split = os.path.join(self.books, 'split')
        write(self.policy, {'ledger_path': self.ledger, 'ledger_dir': self.split, 'ledger_repos': ['kit'],
                            'guard_mcp_tools': ['mcp__x__write'], 'guard_mcp_prefix': 'mcp__x__'})
        write(self.local, {})
        write(os.path.join(self.home, kuser.DIR_NAME, kuser.FILE_NAME), {})

    def edit(self, path, sub=False):
        from tests.guard.claudehost import decide
        from tests.guard.world import event
        return decide(event('Write', {'file_path': path, 'content': ''}, self.books, sub=sub), self.ctx())

    def test_the_location_is_leftover(self):
        self.assertEqual(self.ctx().config_location().state, kuser.LEFTOVER)

    def test_the_ledger_keys_are_read_and_the_config_stays_broken(self):
        c = self.ctx()
        u = c.user()
        self.assertEqual((u['ledger_path'], u['ledger_dir']), (self.ledger, self.split))
        self.assertEqual(u['broken'], c.config_location().error)
        # only the ledger keys: no other key (string or string list, which the tolerant reader
        # keeps) is taken from the refused config
        self.assertEqual((u['guard_mcp_tools'], u['guard_mcp_prefix'], u['ledger_repos']), (None, None, None))

    def test_a_main_session_edit_of_the_ledger_is_refused(self):
        for path in (self.ledger, os.path.join(self.split, 'rules', 'kit.md'), os.path.join(self.split, 'STANDING.md')):
            d = self.edit(path)
            self.assertIsNotNone(d, path)
            self.assertEqual(d.rule, 'ledger', d.reason)

    def test_every_subagent_write_is_still_refused(self):
        d = self.edit(os.path.join(self.books, 'a.py'), sub=True)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn('broken', d.reason)


class HubFoldersTest(_Case):
    def test_the_home_the_located_and_the_pointer_folders_are_held(self):
        got = [os.path.normcase(p) for p, _real in rules.hub_secret_folders(self.ctx())]
        self.assertIn(os.path.normcase(os.path.join(self.home, kuser.DIR_NAME, 'hub')), got)
        self.assertIn(os.path.normcase(os.path.join(self.folder, 'hub')), got)
        self.assertEqual(len(got), len(set(got)))


class RefusalLogLocationTest(_Case):
    def test_the_location_is_read_with_no_git_child(self):
        with self.no_git():
            loc = refusal_log._location(self.home)
        self.assertEqual((loc.state, loc.pm_data['path']), (kuser.OK, os.path.normpath(self.pm)))

    def test_a_pointer_the_filesystem_cannot_check_is_bad(self):
        with self.no_git(), mock.patch.object(place, 'classify', side_effect=lambda path, fallback: fallback(path)):
            loc = refusal_log._location(self.home)
        self.assertEqual(loc.state, kuser.BAD)
        self.assertIsNone(loc.pm_data)

    def test_a_head_the_file_cannot_tell_is_bad_not_asked_of_git(self):
        with self.no_git(), mock.patch.object(place, 'head_branch', return_value=None):
            loc = refusal_log._location(self.home)
        self.assertEqual(loc.state, kuser.BAD)
        self.assertIn('HEAD file cannot tell', loc.error)


if __name__ == '__main__':
    unittest.main()
