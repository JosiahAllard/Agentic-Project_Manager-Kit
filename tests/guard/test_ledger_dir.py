"""The guard's ledger keys (kitlib.guard.context Ctx.user): a blank ledger_dir or ledger_path
(unset: nothing to hold), one not absolute once read against the PM repository, or a ledger_dir
that names something other than a folder (both still held as given) makes the machine config
broken, so every subagent write is refused, never a silently unguarded ledger; a folder that does not exist yet and one outside the
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


class BlankTest(_Case):
    """A blank ledger key: nothing to hold, so it is unset; broken, every subagent write refused."""

    def check(self, key, value):
        body = {key: value}
        if key == 'ledger_dir':
            body['ledger_repos'] = ['kit']
        self.config(body)
        u = self.ctx().user()
        self.assertIsNone(u[key])
        self.assertIn(key, u.get('broken') or '')
        self.assertIn('non-empty', u.get('broken'))
        d = self.assert_subagent_refused()
        self.assertIn(key, d.reason)

    def test_a_blank_ledger_dir(self):
        self.check('ledger_dir', '  ')

    def test_an_empty_ledger_path(self):
        self.check('ledger_path', '')


class DriveRelativeTest(_Case):
    """A ledger key relative to a drive's current folder (`Q:books`): broken, and still held as
    given, as before the check, so a main session's write of it is refused by `ledger` too. The
    drive is one the PM repository is not on: `resolved` would read a same-drive one against it."""

    def drive(self):
        here = os.path.splitdrive(self.pm)[0][:1].upper()
        return 'R:' if here == 'Q' else 'Q:'

    def check(self, key, value, written):
        body = {key: value}
        if key == 'ledger_dir':
            body['ledger_repos'] = ['kit']
        self.config(body)
        c = self.ctx()
        u = c.user()
        self.assertEqual(u[key], value)
        self.assertIn(key, u.get('broken') or '')
        self.assertIn('absolute', u.get('broken'))
        d = self.assert_subagent_refused(ctx=c)
        self.assertIn(key, d.reason)
        d = self.edit(written, ctx=c)
        self.assertIsNotNone(d, written)
        self.assertEqual(d.rule, 'ledger', d.reason)

    def test_a_drive_relative_ledger_dir(self):
        books = self.drive() + 'books'
        self.check('ledger_dir', books, books + '\\STANDING.md')

    def test_a_drive_relative_ledger_path(self):
        ledger = self.drive() + 'LEDGER.md'
        self.check('ledger_path', ledger, ledger)


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

    def test_a_trailing_separator_does_not_hide_the_file(self):
        books = os.path.join(self.pm, 'books')
        with open(books, 'w') as f:
            f.write('')
        for value in (books + '\\', books + '/'):
            self.config({'ledger_dir': value, 'ledger_repos': ['kit']})
            u = self.ctx().user()
            self.assertEqual(u['ledger_dir'], value)
            self.assertIn('not a folder', u.get('broken') or '', value)


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

    def test_a_pointer_config_that_gives_no_ledger_key_adds_nothing(self):
        bodies = {
            'not JSON': '{"ledger_dir": ',
            'not UTF-8': b'\xff\xfe\x00{',
            'not an object': json.dumps(['ledger_dir', 'books']),
            'keys not strings': json.dumps({'ledger_path': 5, 'ledger_dir': ['books'], 'ledger_repos': ['kit']}),
        }
        for what, body in bodies.items():
            with open(self.policy, 'wb') as f:
                f.write(body if isinstance(body, bytes) else body.encode('utf-8'))
            c = self.ctx()
            self.assertEqual(c.config_location().state, kuser.BAD, what)
            u = c.user()
            self.assertEqual((u['ledger_path'], u['ledger_dir']), (None, None), what)
            self.assertEqual(u.get('broken'), c.config_location().error, what)
            self.assert_subagent_refused(ctx=c)

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


class UnreadTest(_Case):
    """A ledger key the guard could not read (Ctx.user 'ledger_unread'): the ledger may be any path,
    so a main session's every write is asked of the owner (rule ledger, verdict ask), never let
    through; a subagent's is denied; a path still held is denied as before. ledger.py refuses a
    broken config, so no ledger write of the kit's is asked."""

    ROLES = ('owner', 'pm')

    def ctx(self, role='owner', **kw):
        return super().ctx(role_resolver=lambda action, environ: role, **kw)

    def run_(self, payload, role='owner', ctx=None):
        from tests.guard.claudehost import decide
        return decide(payload, ctx or self.ctx(role))

    def bad_unread(self):
        """A BAD location whose pointer config is not JSON: nothing of the ledger can be read."""
        self.config({'ledger_path': 'LEDGER.md'})
        with open(self.policy, 'w', encoding='utf-8') as f:
            f.write('{"ledger_path": ')
        with open(os.path.join(self.pm, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/other\n')

    def assert_asked(self, payload, what):
        for role in self.ROLES:
            d = self.run_(payload, role)
            self.assertIsNotNone(d, '%s (%s)' % (what, role))
            self.assertEqual((d.verdict, d.rule), ('ask', 'ledger'), '%s (%s): %s' % (what, role, d.reason))
            self.assertIn('broken', d.reason)

    def write(self, path, sub=False):
        from tests.guard.world import event
        return event('Write', {'file_path': path, 'content': ''}, self.pm, sub=sub)

    def test_the_marker(self):
        self.bad_unread()
        self.assertTrue(self.ctx().user().get('ledger_unread'))
        self.config({'ledger_path': ''})
        self.assertTrue(self.ctx().user().get('ledger_unread'))
        self.config({'ledger_path': os.path.join(self.pm, 'LEDGER.md')})
        self.assertFalse(self.ctx().user().get('ledger_unread'))

    def test_bad_location_with_no_readable_ledger_key_asks_a_main_session(self):
        self.bad_unread()
        for path in (os.path.join(self.pm, 'LEDGER.md'), os.path.join(self.pm, 'a.py')):
            self.assert_asked(self.write(path), path)
        # outside the PM session's jail: the owner is asked, the PM session's write-jail refusal wins
        work = os.path.join(self.work, 'notes.md')
        d = self.run_(self.write(work), 'owner')
        self.assertEqual((d.verdict, d.rule), ('ask', 'ledger'), d.reason)
        self.assert_denied_as(self.write(work), 'pm', 'write-jail', work)

    def test_bad_location_with_a_relative_pointer_asks_a_main_session(self):
        self.config({'ledger_path': 'LEDGER.md'})
        with open(os.path.join(self.pm, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/other\n')
        try:
            relative = os.path.relpath(self.pm)
        except ValueError:
            self.skipTest('the PM repository is on another drive than the current folder')
        bad = super().ctx().config_location()
        self.assertEqual(bad.state, kuser.BAD)
        for role in self.ROLES:
            c = self.ctx(role, location=lambda: bad, pointer=lambda: relative)
            d = self.run_(self.write(os.path.join(self.pm, 'LEDGER.md')), role, ctx=c)
            self.assertIsNotNone(d, role)
            self.assertEqual((d.verdict, d.rule), ('ask', 'ledger'), d.reason)

    def test_a_blank_only_key_asks_a_main_session(self):
        self.config({'ledger_path': ''})
        self.assert_asked(self.write(os.path.join(self.pm, 'a.py')), 'a.py')

    def test_a_blank_key_beside_a_held_key_asks_and_the_held_path_stays_denied(self):
        books = os.path.join(self.pm, 'books')
        self.config({'ledger_path': '  ', 'ledger_dir': books, 'ledger_repos': ['kit']})
        self.assertTrue(self.ctx().user().get('ledger_unread'))
        self.assert_asked(self.write(os.path.join(self.pm, 'FROZEN.md')), 'FROZEN.md')
        d = self.run_(self.write(os.path.join(books, 'STANDING.md')))
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)

    def test_a_shell_write_asks_a_main_session(self):
        from tests.guard.world import bash
        self.bad_unread()
        self.assert_asked(bash('rm "%s"' % os.path.join(self.pm, 'LEDGER.md').replace('\\', '/'), self.pm), 'rm')

    def test_git_writes_ask_a_main_session(self):
        from tests.guard.world import bash
        self.bad_unread()
        # a folder outside the PM repository: the ledger may be in any tree, so no place is needed
        work = self.work.replace('\\', '/')
        for cmd in ('git -C "%s" rm LEDGER.md' % work, 'git -C "%s" checkout -- .' % work,
                    'git -C "%s" restore LEDGER.md' % work, 'git rm LEDGER.md'):
            self.assert_asked(bash(cmd, self.work), cmd)

    def test_a_git_read_passes_a_main_session(self):
        from tests.guard.world import bash
        self.bad_unread()
        self.assertIsNone(self.run_(bash('git -C "%s" status' % self.pm.replace('\\', '/'), self.work)))

    def test_a_subagent_is_denied(self):
        from tests.guard.world import bash
        self.bad_unread()
        d = self.run_(self.write(os.path.join(self.pm, 'a.py'), sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        d = self.run_(bash('git -C "%s" rm LEDGER.md' % self.pm.replace('\\', '/'), self.work, sub=True))
        self.assertIsNotNone(d)
        self.assertEqual(d.verdict, 'deny', d.reason)

    def test_a_held_ledger_under_a_broken_config_is_still_denied(self):
        self.config({'ledger_path': 'LEDGER.md'})
        with open(os.path.join(self.pm, '.git', 'HEAD'), 'w') as f:
            f.write('ref: refs/heads/other\n')
        self.assertFalse(self.ctx().user().get('ledger_unread'))
        d = self.run_(self.write(os.path.join(self.pm, 'LEDGER.md')))
        self.assertEqual((d.verdict, d.rule), ('deny', 'ledger'), d.reason)
        self.assertIsNone(self.run_(self.write(os.path.join(self.work, 'notes.md'))))

    def test_a_sound_config_with_no_ledger_lets_a_main_session_write(self):
        self.config({})
        self.assertIsNone(self.run_(self.write(os.path.join(self.work, 'notes.md'))))

    # Any refusal wins over the unread ask: the ask holds back until every other rule has judged
    # the call (edit_target, ledger_git, shell_command, shell_writes; rules.Asked).

    def assert_denied_as(self, payload, role, rule, what):
        d = self.run_(payload, role)
        self.assertIsNotNone(d, what)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), '%s: %s' % (what, d.reason))

    def test_a_protected_file_stays_denied_with_the_marker(self):
        self.bad_unread()
        self.assertTrue(self.ctx('coordinator').user().get('ledger_unread'))
        self.assert_denied_as(self.write(self.policy), 'coordinator', 'machine-config', 'the machine config')

    def test_a_main_checkout_write_stays_denied_with_the_marker(self):
        self.bad_unread()
        with open(os.path.join(self.pm, '.kit.json'), 'w', encoding='utf-8') as f:
            json.dump({'repo': {'name': 'pm'}, 'roles': {'main': {'edit_main_checkout': False, 'why': 'test'}}}, f)
        self.assert_denied_as(self.write(os.path.join(self.pm, 'a.py')), 'owner', 'main-checkout', 'a.py')

    def test_git_rm_of_a_held_path_stays_denied_with_the_marker(self):
        from tests.guard.world import bash
        books = os.path.join(self.pm, 'books')
        self.config({'ledger_path': '', 'ledger_dir': books, 'ledger_repos': ['kit']})
        self.assertTrue(self.ctx().user().get('ledger_unread'))
        cmd = 'git -C "%s" rm books/STANDING.md' % self.pm.replace('\\', '/')
        for role in self.ROLES:
            self.assert_denied_as(bash(cmd, self.pm), role, 'ledger', cmd)

    def test_a_git_write_outside_the_jail_stays_denied_with_the_marker(self):
        from tests.guard.world import bash
        self.bad_unread()
        cmd = 'git -C "%s" rm notes.md' % self.work.replace('\\', '/')
        d = self.run_(bash(cmd, self.pm), 'pm')         # the PM session's jail: its checkout, the PM repository
        self.assertIsNotNone(d, cmd)
        self.assertEqual(d.verdict, 'deny', d.reason)
        self.assertNotEqual(d.rule, 'ledger', d.reason)

    def test_a_computed_bash_target_from_a_worktree_asks(self):
        from tests.guard.world import bash
        self.bad_unread()
        self.assert_asked(bash('rm "$x"', self.work), 'rm "$x"')


if __name__ == '__main__':
    unittest.main()
