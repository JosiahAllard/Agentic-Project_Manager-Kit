"""The guard's ledger keys (kitlib.guard.context Ctx.user): a ledger_dir or ledger_path the guard
cannot place (blank, or not absolute once read against the PM repository) or a ledger_dir that
names something other than a folder makes the machine config broken, so every subagent write is
refused, never a silently unguarded ledger; a folder that does not exist yet and one outside the
PM repository are accepted. Under a BAD machine config location the ledger keys, and only those,
are read from the folder the PM-repo pointer names when it is absolute, so a main session's edit
of the ledger is refused there too. Every home and PM repository is a temp folder under the fence
(tests.home)."""
import json
import os
import tempfile
import unittest

from kitlib.config import user as kuser
from kitlib.guard import context
from tests import gitfixture
from tests.home import pm_repo, temp_home


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps(body))


class _Case(unittest.TestCase):
    def setUp(self):
        self.home = temp_home(self)
        self.pm = pm_repo(self)
        self.folder = os.path.join(self.pm, kuser.DIR_NAME)
        self.policy = os.path.join(self.folder, kuser.FILE_NAME)
        self.kit = tempfile.mkdtemp(prefix='kit-guard-kit-')
        self.addCleanup(gitfixture.rmtree, self.kit)
        self.work = tempfile.mkdtemp(prefix='kit-guard-work-')
        self.addCleanup(gitfixture.rmtree, self.work)

    def ctx(self, **kw):
        return context.Ctx(environ=dict(os.environ), home=self.home, kit=self.kit, **kw)

    def config(self, body):
        write(self.policy, body)
        write(os.path.join(self.folder, kuser.LOCAL_FILE_NAME), {})

    def edit(self, path, sub=False, ctx=None):
        from tests.guard.claudehost import decide
        from tests.guard.world import event
        return decide(event('Write', {'file_path': path, 'content': ''}, self.pm, sub=sub), ctx or self.ctx())

    def assert_subagent_refused(self, ctx=None):
        d = self.edit(os.path.join(self.pm, 'a.py'), sub=True, ctx=ctx)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)
        self.assertIn('broken', d.reason)
        return d


class UnplacedTest(_Case):
    """A ledger key the guard cannot place: broken, the key unset, every subagent write refused."""

    def check(self, key, value, said):
        body = {key: value}
        if key == 'ledger_dir':
            body['ledger_repos'] = ['kit']
        self.config(body)
        u = self.ctx().user()
        self.assertIsNone(u[key])
        self.assertIn(key, u.get('broken') or '')
        self.assertIn(said, u.get('broken'))
        d = self.assert_subagent_refused()
        self.assertIn(key, d.reason)

    def test_a_drive_relative_ledger_dir(self):
        self.check('ledger_dir', 'C:books', 'absolute')

    def test_a_drive_relative_ledger_path(self):
        self.check('ledger_path', 'C:LEDGER.md', 'absolute')

    def test_a_blank_ledger_dir(self):
        self.check('ledger_dir', '  ', 'non-empty')

    def test_an_empty_ledger_path(self):
        self.check('ledger_path', '', 'non-empty')


class NotAFolderTest(_Case):
    def test_a_ledger_dir_that_is_a_file_is_broken_and_still_held(self):
        books = os.path.join(self.pm, 'books')
        with open(books, 'w') as f:
            f.write('')
        self.config({'ledger_dir': books, 'ledger_repos': ['kit']})
        u = self.ctx().user()
        self.assertEqual(u['ledger_dir'], books)
        self.assertIn('not a folder', u.get('broken') or '')
        self.assert_subagent_refused()


class AcceptedTest(_Case):
    """What stays a good ledger_dir: a folder ledger.py has not made yet, one outside the PM
    repository, one written relative to it. Not broken, and its digest held for every session."""

    def check(self, value, expected):
        self.config({'ledger_dir': value, 'ledger_repos': ['kit']})
        c = self.ctx()
        u = c.user()
        self.assertEqual(u['ledger_dir'], expected)
        self.assertFalse(u.get('broken'))
        d = self.edit(os.path.join(expected, 'STANDING.md'), ctx=c)
        self.assertIsNotNone(d)
        self.assertEqual(d.rule, 'ledger', d.reason)

    def test_a_folder_that_does_not_exist_yet(self):
        self.check(os.path.join(self.pm, 'books'), os.path.join(self.pm, 'books'))

    def test_a_folder_outside_the_pm_repository(self):
        self.check(self.work, self.work)

    def test_a_folder_relative_to_the_pm_repository(self):
        self.check('books', os.path.join(self.pm, 'books'))


class BadStateTest(_Case):
    """A BAD location (here the PM repository is off its data branch): the config stays broken,
    and the ledger keys of the folder the pointer names are read, so a main session's edit of the
    ledger is refused; no other key is taken from it."""

    def setUp(self):
        super().setUp()
        self.books = os.path.join(self.pm, 'books')
        self.ledger = os.path.join(self.pm, 'LEDGER.md')
        self.config({'ledger_path': 'LEDGER.md', 'ledger_dir': 'books', 'ledger_repos': ['kit'],
                     'guard_mcp_tools': ['mcp__x__write'], 'guard_mcp_prefix': 'mcp__x__'})
        with open(os.path.join(self.pm, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/other\n')

    def test_the_location_is_bad(self):
        self.assertEqual(self.ctx().config_location().state, kuser.BAD)

    def test_the_ledger_keys_are_read_against_the_pointer_and_the_config_stays_broken(self):
        c = self.ctx()
        u = c.user()
        self.assertEqual((u['ledger_path'], u['ledger_dir']), (self.ledger, self.books))
        self.assertEqual(u.get('broken'), c.config_location().error)
        self.assertEqual((u['guard_mcp_tools'], u['guard_mcp_prefix'], u['ledger_repos']), (None, None, None))

    def test_a_main_session_edit_of_the_ledger_is_refused(self):
        for path in (self.ledger, os.path.join(self.books, 'rules', 'kit.md'), os.path.join(self.books, 'STANDING.md')):
            d = self.edit(path)
            self.assertIsNotNone(d, path)
            self.assertEqual(d.rule, 'ledger', d.reason)

    def test_every_subagent_write_is_still_refused(self):
        self.assert_subagent_refused()

    def test_a_pointer_that_is_not_absolute_is_not_read(self):
        # the PM repository itself, written relative to this process's folder: read as it stands,
        # it would find the files, so only the absolute check keeps them unread
        try:
            relative = os.path.relpath(self.pm)
        except ValueError:
            self.skipTest('the PM repository is on another drive than the current folder')
        bad = self.ctx().config_location()
        c = self.ctx(location=lambda: bad, pointer=lambda: relative)
        u = c.user()
        self.assertEqual((u['ledger_path'], u['ledger_dir']), (None, None))
        self.assertEqual(u.get('broken'), bad.error)


if __name__ == '__main__':
    unittest.main()
