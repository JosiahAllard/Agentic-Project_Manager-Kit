"""PowerShell provider paths on drives that hold no files (env:, variable:, function:, alias:,
HKCU:, HKLM:, Cert:, WSMan: and the Provider:: spellings): a cmdlet that writes one writes no
file, so the main-checkout rule and the write jail do not judge it (kitlib/guard/rules.py
ps_provider_item). A real file write, a drive-letter path, a relative path, a computed item
and a line that may remap a drive are judged as before."""
import unittest

from kitlib.guard import decide

from . import world as W
from .world import ps


class ProviderItems(unittest.TestCase):
    """Writes to a non-file provider drive, run in the main checkout by a subagent (a role
    without edit_main_checkout): not refused."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_env_items_in_the_main_checkout(self):
        for text in ('New-Item -Path env:FOO -Value x',
                     'New-Item -Path Env:\\FOO -Value x',
                     'New-Item -Path ENV:/FOO -Value x',
                     'New-Item env:FOO -Value x',
                     'ni -Path:env:FOO -Value x',
                     "New-Item -Path 'env:FOO' -Value x",
                     'New-Item -Path env: -Name FOO -Value x',
                     'New-Item -Path Env:\\ -Name FOO -Value x',
                     'Set-Content env:FOO x',
                     "Set-Content -Path 'Env:\\FOO' -Value x",
                     'Add-Content -Path env:FOO -Value x',
                     'New-Item -Path Environment::FOO -Value x',
                     'New-Item -Path Microsoft.PowerShell.Core\\Environment::FOO -Value x'):
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNone(d, d and d.reason)

    def test_other_non_file_drives_in_the_main_checkout(self):
        for text in ('New-Item -Path variable:foo -Value x',
                     'Set-Content Variable:\\foo x',
                     'New-Item -Path function:foo -Value "1"',
                     'New-Item -Path alias:gx -Value Get-ChildItem',
                     'New-Item -Path HKCU:\\Software\\Demo',
                     'New-Item -Path hklm:/SOFTWARE/Demo',
                     'New-Item -Path Registry::HKEY_CURRENT_USER\\Software\\Demo',
                     'New-Item -Path Cert:\\CurrentUser\\My\\Demo',
                     'New-Item -Path WSMan:\\localhost\\Demo'):
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNone(d, d and d.reason)


class FileWritesStillJudged(unittest.TestCase):
    """What writes or may write a file in the main checkout stays refused by main-checkout."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def refused(self, text, cwd):
        d = decide(ps(text, cwd, sub=True), self.w.ctx())
        self.assertIsNotNone(d, 'allowed: %s' % text)
        self.assertEqual(d.rule, 'main-checkout', d.reason)

    def test_file_paths(self):
        m = self.w.main
        for text, cwd in (('New-Item -Path %s\\x.txt' % m, self.w.wt),
                          ('New-Item -Path %s\\x.txt -Value x' % m, self.w.main),
                          ('New-Item -Path %s -Name x.txt' % m, self.w.wt),
                          ('Set-Content -Path %s\\x.txt -Value 1' % m, self.w.wt),
                          ('New-Item -Path x.txt', self.w.main),
                          ('New-Item -Path .\\env\\x.txt', self.w.main),
                          ('New-Item -Path envx:\\x.txt', self.w.main),
                          ('New-Item -Path C:x.txt', self.w.main),
                          ('New-Item -Path Temp:\\x.txt', self.w.main),
                          ('New-Item -Path FileSystem::x.txt', self.w.main)):
            with self.subTest(text, cwd=cwd):
                self.refused(text, cwd)

    def test_items_the_guard_cannot_pin_to_a_non_file_drive(self):
        for text in ('New-Item -Path "env:$n" -Value x',
                     "$n='x'; New-Item -Path env: -Name $n -Value x",
                     'New-Item -Path env:..\\x.txt',
                     'New-Item -Path env:\\..\\x.txt',
                     'New-PSDrive -Name Q -PSProvider FileSystem -Root .; New-Item -Path env:FOO',
                     'Remove-PSDrive env; New-Item -Path env:\\x.txt',
                     'ndr env FileSystem .; New-Item -Path env:\\x.txt',
                     'Remove-PSDrive env; & { New-Item -Path env:\\x.txt }',
                     '& { Remove-PSDrive env }; New-Item -Path env:\\x.txt',
                     '$(Remove-PSDrive env); New-Item -Path env:\\x.txt',
                     'Invoke-Expression $s; New-Item -Path env:\\x.txt',
                     'Import-Module .\\m.psm1; New-Item -Path env:\\x.txt',
                     '. .\\setup.ps1; New-Item -Path env:\\x.txt'):
            with self.subTest(text):
                self.refused(text, self.w.main)


if __name__ == '__main__':
    unittest.main()
