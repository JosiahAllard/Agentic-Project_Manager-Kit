"""PowerShell provider and drive paths (shell.resolve_path with shell PS, shell.ps_unplaceable,
rules.shell_writes, rules.ps_drive_jail): from a worktree, a write PowerShell sends into the main
checkout through FileSystem::, a module-qualified FileSystem::, a drive named by anything but one
ASCII letter (New-PSDrive Main FileSystem <main>, 1:, _:) or another provider, or after a cd into
such a place, is refused, never read as a file in the worktree. Bash and cmd words, one-letter
drives and relative paths are read as before."""
import os
import unittest

from kitlib.guard import context
from kitlib.guard import shell as sh

from . import world as W
from .claudehost import decide

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
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
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
        self.assertIn('the directory it runs in is a PowerShell drive', d.reason)

    def test_drive_named_by_anything_but_one_ascii_letter(self):
        # New-PSDrive takes names such as 1, _ or a non-ASCII letter: not a one-letter drive
        for text in (r'New-Item 1:\x.txt', r'New-Item _:\x.txt', 'New-Item é:\\x.txt',
                     r'New-PSDrive 1 FileSystem {main}; New-Item 1:\x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('cannot place', d.reason)

    def test_cd_drive_named_by_anything_but_one_ascii_letter_then_write(self):
        for text in (r'cd 1:\; New-Item x.txt', r'cd _:\; New-Item x.txt', 'cd é:\\; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('Name a literal path inside your worktree', d.reason)

    def test_literal_path_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item -LiteralPath FileSystem::{main}\x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_set_location_and_push_location_filesystem_provider_then_write(self):
        for text in (r'Set-Location FileSystem::{main}; New-Item x.txt',
                     r'Set-Location -LiteralPath FileSystem::{main}; New-Item x.txt',
                     r'Push-Location FileSystem::{main}; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('is in the main checkout', d.reason)

    def test_jailed_session_cd_into_unplaceable_place_then_write(self):
        # cwd and target are both None after the cd: the jail still refuses (track's cwd_ps_drive)
        for text in (r'cd Main:\; New-Item x.txt', r'cd Registry::HKCU\x; New-Item x',
                     r'Set-Location -Path Main:\; New-Item x.txt', r'Push-Location 1:\; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text, env=COORDINATOR, sub=False), 'write-jail')
                self.assertIn('PowerShell drive or provider path', d.reason)

    def test_unknown_provider(self):
        # a provider the guard does not know (a module's own, or FileSystem from another module)
        # may hold files anywhere; the built-in non-file ones (Registry::, Environment::) are
        # left to kit/guard-env-drive's skip and are not pinned here
        for text in (r'New-Item Demo.Provider::{main}\x.txt', r'New-Item Other.Module\FileSystem::{main}\x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('cannot place', d.reason)

    def test_jailed_session_that_may_edit_main_is_refused_by_the_jail(self):
        # a coordinator's main session may edit a main checkout, so main_checkout lets an unplaced
        # target through; the jail cannot judge it and refuses (ps_drive_jail)
        d = self.denied(self.run_ps(r'New-Item Main:\x.txt', env=COORDINATOR, sub=False), 'write-jail')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_root_search_through_filesystem_provider(self):
        drive = os.path.splitdrive(self.w.base)[0]
        self.denied(self.run_ps('Get-ChildItem FileSystem::%s\\ -Recurse' % drive), 'root-search')


class DynamicTargets(Base):
    """A PowerShell write target the guard cannot read as a literal ($p, "...$n...", $env:TEMP, a
    ( ) expression), from a worktree: refused, where before it was skipped and allowed
    (rules.shell_writes). $PWD and a literal rest is read against the tracked location
    (rules.ps_pwd_path); a ( ) target fails closed from a main checkout too (shell._Scan.ps_paren_arg)."""

    def test_variable_target_into_main(self):
        d = self.denied(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x'), 'main-checkout')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_jailed_session_variable_target_is_refused_by_the_jail(self):
        self.denied(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x', env=COORDINATOR, sub=False), 'write-jail')

    def test_variable_inside_a_literal_path(self):
        self.denied(self.run_ps(r'Set-Content "{wt}\$n.txt" x'), 'main-checkout')

    def test_env_temp_target(self):
        self.denied(self.run_ps(r'Set-Content $env:TEMP\x.txt x'), 'main-checkout')

    def test_pwd_and_a_literal_rest_is_read_against_the_location(self):
        self.allowed(self.run_ps(r'Set-Content "$PWD\x.txt" x'))
        self.allowed(self.run_ps(r'Set-Content "${{PWD}}\x.txt" x'))
        d = self.denied(self.run_ps(r'cd {main}; Set-Content "$PWD\x.txt" x'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_pwd_the_line_may_set_fails_closed(self):
        for text in (r'$PWD = "{main}"; Set-Content "$PWD\x.txt" x',
                     r'Set-Variable PWD {main}; Set-Content "$PWD\x.txt" x'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')

    def test_parenthesised_target(self):
        for text in (r'Set-Content (Join-Path {main} x.txt) x',
                     r'Set-Content -Path:(Join-Path {main} x.txt) x',
                     r'Remove-Item ("{main}\x.txt")'):
            for cwd in (self.w.wt, self.w.main):
                with self.subTest(text=text, cwd=cwd):
                    ev = W.ps(text.format(main=self.w.main), cwd, True)
                    self.denied(decide(ev, self.w.ctx()), 'main-checkout')

    def test_cd_inside_a_subexpression_makes_the_location_unknown(self):
        # $( ) and ( ) run in the caller's scope: the cd moves the location for what follows
        for text in (r'echo $(cd {main}) ; New-Item x.txt', r'echo (cd {main}) ; New-Item x.txt'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')


class Unchanged(Base):
    """What the fix leaves as it was."""

    def test_dynamic_targets_outside_powershell_and_reads(self):
        self.allowed(self.run_bash('p={main}/x.txt; echo 1 > "$p"'))
        self.allowed(self.run_ps(r'git status 2>$null'))
        self.allowed(self.run_ps(r'$p="{main}\x.txt"; Get-Content $p'))

    def test_owner_session_dynamic_target_is_not_refused(self):
        self.allowed(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x', sub=False))

    def test_parenthesised_values_that_are_not_targets(self):
        for text in (r'New-Item x.txt -Value (Get-Date)', r'Write-Output (Get-Date)',
                     r'$x = (Get-Date); New-Item y.txt', r'if (Test-Path x) {{ New-Item y.txt }}'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

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
        self.allowed(self.run_ps(r'cd Main:\; New-Item x.txt', sub=False))

    def test_jailed_session_cd_back_to_a_known_place_writes_as_before(self):
        self.allowed(self.run_ps(r'cd Main:\; cd {wt}; New-Item x.txt', env=COORDINATOR, sub=False))

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
        for text in ('Main:\\x', 'Main:x', 'Registry::x', 'Foo\\Bar::x', 'FileSystem::rel', 'env:FOO',
                     '1:\\x', '_:x', 'é:\\x', '12:\\x'):
            with self.subTest(text):
                self.assertTrue(sh.ps_unplaceable(text))
        for text in ('', 'C:\\x', 'C:x', 'x.txt', '.\\a:b', '\\\\srv\\s', '//srv/s',
                     'FileSystem::C:\\x', 'Microsoft.PowerShell.Core\\FileSystem::\\\\srv\\s'):
            with self.subTest(text):
                self.assertFalse(sh.ps_unplaceable(text))


if __name__ == '__main__':
    unittest.main()
