"""PowerShell provider and drive paths (shell.resolve_path with shell PS, shell.ps_unplaceable,
rules.shell_writes, rules.ps_drive_jail): from a worktree, a write PowerShell sends into the main
checkout through FileSystem::, a module-qualified FileSystem::, a drive named by more than one
letter (New-PSDrive Main FileSystem <main>) or another provider is refused, never read as a file
in the worktree. Bash and cmd words, one-letter drives and relative paths are read as before."""
import os
import unittest

from kitlib.guard import context, decide
from kitlib.guard import shell as sh

from . import world as W

COORDINATOR = {context.ROLE_ENV: 'coordinator'}     # a jailed main session: it may edit a main checkout


def missing_drive():
    """A drive letter with no drive behind it on this machine."""
    for letter in 'QRMNOPSTUVWXY':
        if not os.path.isdir(letter + ':\\'):
            return letter
    raise unittest.SkipTest('every drive letter tried is in use')


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def run_ps(self, text, env=None, sub=True):
        text = text.format(main=self.w.main, wt=self.w.wt)
        return decide(W.ps(text, self.w.wt, sub), self.w.ctx(environ=env or {}))

    def run_bash(self, text):
        text = text.format(main=W.fwd(self.w.main), wt=W.fwd(self.w.wt))
        return decide(W.bash(text, self.w.wt, True), self.w.ctx())

    def denied(self, d, rule):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.kind, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)


class IntoMain(Base):
    """Each spelling, from a worktree, of a PowerShell write that lands in the main checkout or a
    place the guard cannot tell: allowed before the fix (the text was joined to the worktree)."""

    def test_new_item_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item FileSystem::{main}\x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_copy_item_filesystem_provider(self):
        self.denied(self.run_ps(r'Copy-Item a.txt FileSystem::{main}\x.txt'), 'main-checkout')

    def test_redirect_filesystem_provider(self):
        self.denied(self.run_ps(r'echo 1 > FileSystem::{main}\x.txt'), 'main-checkout')

    def test_module_qualified_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item Microsoft.PowerShell.Core\FileSystem::{main}\x.txt'),
                        'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_multi_letter_drive(self):
        d = self.denied(self.run_ps(r'New-PSDrive Main FileSystem {main}; New-Item Main:\x.txt'), 'main-checkout')
        self.assertIn('cannot place: Main:\\x.txt', d.reason)

    def test_cd_filesystem_provider_then_relative_write(self):
        d = self.denied(self.run_ps(r'cd FileSystem::{main}; New-Item x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_cd_multi_letter_drive_then_relative_write(self):
        d = self.denied(self.run_ps(r'cd Main:\; New-Item x.txt'), 'main-checkout')
        self.assertIn('the directory it runs in is unknown', d.reason)

    def test_unknown_provider(self):
        d = self.denied(self.run_ps(r'New-Item Registry::HKEY_CURRENT_USER\Software\x'), 'main-checkout')
        self.assertIn('cannot place', d.reason)

    def test_jailed_session_that_may_edit_main_is_refused_by_the_jail(self):
        # a coordinator's main session may edit a main checkout, so main_checkout lets an unplaced
        # target through; the jail cannot judge it and refuses (ps_drive_jail)
        d = self.denied(self.run_ps(r'New-Item Main:\x.txt', env=COORDINATOR, sub=False), 'write-jail')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_root_search_through_filesystem_provider(self):
        drive = os.path.splitdrive(self.w.base)[0]
        self.denied(self.run_ps('Get-ChildItem FileSystem::%s\\ -Recurse' % drive), 'root-search')


class Unchanged(Base):
    """What the fix leaves as it was."""

    def test_missing_one_letter_drive_still_refused(self):
        letter = missing_drive()
        self.denied(self.run_ps('New-Item %s:\\x.txt' % letter), 'write-jail')

    def test_filesystem_provider_into_the_worktree_is_allowed(self):
        self.allowed(self.run_ps(r'New-Item FileSystem::{wt}\x.txt'))
        self.allowed(self.run_ps(r'echo 1 > Microsoft.PowerShell.Core\FileSystem::{wt}\x.txt'))

    def test_relative_and_absolute_writes(self):
        self.allowed(self.run_ps(r'New-Item x.txt'))
        self.allowed(self.run_ps(r'New-Item src\x.txt'))
        self.allowed(self.run_ps(r'New-Item {wt}\x.txt'))
        self.denied(self.run_ps(r'New-Item {main}\x.txt'), 'main-checkout')

    def test_owner_session_is_not_refused(self):
        self.allowed(self.run_ps(r'New-Item Main:\x.txt', sub=False))

    def test_bash_colon_words_are_files_in_the_worktree(self):
        self.allowed(self.run_bash('echo 1 > Main:x.txt'))
        self.allowed(self.run_bash('echo 1 > "FileSystem::{main}/x.txt"'))
        self.allowed(self.run_bash('touch C::x'))


class ResolvePath(unittest.TestCase):
    """shell.resolve_path and shell.ps_unplaceable on their own."""

    CWD = 'C:\\w\\wt'

    def test_bash_and_cmd_unchanged(self):
        for shell in (None, sh.BASH, sh.CMD):
            with self.subTest(shell):
                self.assertEqual(sh.resolve_path('FileSystem::C:\\m\\x', self.CWD, shell),
                                 'C:\\w\\wt\\FileSystem::C:\\m\\x')
                self.assertEqual(sh.resolve_path('Main:\\x', self.CWD, shell), 'C:\\w\\wt\\Main:\\x')
                self.assertEqual(sh.resolve_path('C::x', self.CWD, shell), None)
                self.assertEqual(sh.resolve_path('C:\\m\\x', self.CWD, shell), 'C:\\m\\x')

    def test_powershell(self):
        cases = {
            'FileSystem::C:\\m\\x': 'C:\\m\\x',
            'filesystem::C:/m/x': 'C:\\m\\x',
            'Microsoft.PowerShell.Core\\FileSystem::C:\\m\\x': 'C:\\m\\x',
            'FileSystem::\\\\srv\\share\\x': '\\\\srv\\share\\x',
            'FileSystem::x.txt': None,              # relative: whose current folder is not shown
            'Other.Module\\FileSystem::C:\\m\\x': None,
            'Registry::HKCU\\x': None,
            'Main:\\x': None,
            'Temp:\\x': None,
            'env:FOO': None,
            'C:x': None,                            # one-letter drive-relative: as before
            'C:\\m\\x': 'C:\\m\\x',
            'x.txt': 'C:\\w\\wt\\x.txt',
            '.\\a:b': 'C:\\w\\wt\\a:b',
            '\\\\srv\\share\\x': '\\\\srv\\share\\x',
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(sh.resolve_path(text, self.CWD, sh.PS), want)

    def test_unplaceable(self):
        for text in ('Main:\\x', 'Main:x', 'Registry::x', 'Foo\\Bar::x', 'FileSystem::rel', 'env:FOO'):
            with self.subTest(text):
                self.assertTrue(sh.ps_unplaceable(text))
        for text in ('', 'C:\\x', 'C:x', 'x.txt', '.\\a:b', '\\\\srv\\s', '//srv/s',
                     'FileSystem::C:\\x', 'Microsoft.PowerShell.Core\\FileSystem::\\\\srv\\s'):
            with self.subTest(text):
                self.assertFalse(sh.ps_unplaceable(text))


if __name__ == '__main__':
    unittest.main()
