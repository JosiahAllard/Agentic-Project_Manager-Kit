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


class NativePrograms(unittest.TestCase):
    """A native program reads env:FOO as the stream FOO of a file named env in the directory it
    runs in: a file write, judged as one. Only a PowerShell cmdlet's item is skipped."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, text, cwd):
        return decide(ps(text, cwd, sub=True), self.w.ctx())

    def test_native_writers_in_the_main_checkout(self):
        for text in ('curl.exe -o env:FOO https://example.invalid/x',
                     'curl -o env:FOO https://example.invalid/x',
                     'wget -O env:FOO https://example.invalid/x',
                     'xcopy a env:FOO',
                     'robocopy a env:FOO x.txt',
                     'Start-Process curl.exe -ArgumentList "-o", "env:FOO", "https://example.invalid/x"'):
            with self.subTest(text):
                d = self.decide(text, self.w.main)
                self.assertIsNotNone(d, 'allowed: %s' % text)
                self.assertEqual(d.rule, 'main-checkout', d.reason)

    def test_native_writer_outside_the_roots(self):
        text = 'cd "%s"; xcopy a env:FOO' % W.fwd(self.w.plain)
        d = self.decide(text, self.w.wt)
        self.assertIsNotNone(d, 'allowed: %s' % text)
        self.assertEqual(d.rule, 'write-jail', d.reason)


class RemapForms(unittest.TestCase):
    """A line that may remap a drive or run code the text does not show, however it is spelled
    (backticks, concatenation, a computed or called command, a dot-source after a newline, the
    script-running APIs): env:x there may be a file, so it is judged as one."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_hidden_remaps_stay_judged(self):
        item = 'New-Item -Path env:\\x.txt'
        for pre in ('n`dr env FileSystem .', 'r`dr env', 'New-PSDr`ive -Name env -PSProvider FileSystem -Root .',
                    "& ('New-PSDr'+'ive') env FileSystem .", '& (gcm *-PSDr*) env FileSystem .',
                    '$c = "ndr"; & $c env FileSystem .', '$x = 1\n. .\\setup', '. .\\setup', '& .\\script',
                    '$p = [powershell]::Create()', '$ExecutionContext.InvokeCommand.InvokeScript($s)',
                    '$p.AddScript($s)'):
            text = '%s%s%s' % (pre, '\n' if '\n' in pre else '; ', item)
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed: %s' % text)
                self.assertEqual(d.rule, 'main-checkout', d.reason)


if __name__ == '__main__':
    unittest.main()
