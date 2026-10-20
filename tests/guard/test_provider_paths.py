"""PowerShell provider paths on drives that hold no files (env:, variable:, function:, alias:,
HKCU:, HKLM:, Cert:, WSMan: and the Provider:: spellings): a cmdlet that writes one writes no
file, so the main-checkout rule and the write jail do not judge it (kitlib/guard/rules.py
ps_provider_item). A real file write, a drive-letter path, a relative path, a computed item
and a line that may remap a drive are judged as before."""
import unittest

from .claudehost import decide

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
                     'New-Item -Path Microsoft.PowerShell.Core\\Environment::FOO -Value x',
                     'ni env:FOO; sc env:BAR x',
                     'Remove-Item env:FOO',
                     'Rename-Item env:FOO BAR',
                     'Get-Item env:FOO; Clear-Content env:FOO'):
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNone(d, d and d.reason)

    def test_beside_a_plain_git_call(self):
        # git.exe cannot remap a PowerShell drive, and its words meet the git rules
        g = 'git -C "%s" status' % W.fwd(self.w.wt)
        for text, cwd in (('Set-Content env:FOO x; %s' % g, self.w.main),
                          ('Rename-Item env:FOO BAR; Get-Content "$env:TEMP\\x.txt"; %s' % g, self.w.main),
                          ('New-Item -Path env:FOO -Value x\ngit.exe -C "%s" status' % W.fwd(self.w.wt), self.w.main),
                          # the same read from the worktree (a $env:TEMP write target is refused
                          # as not literal: test_ps_drive_paths DynamicTargets)
                          ('Rename-Item env:FOO BAR; Get-Content "$env:TEMP\\x.txt"; %s' % g, self.w.wt)):
            with self.subTest(text, cwd=cwd):
                d = decide(ps(text, cwd, sub=True), self.w.ctx())
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

    def refused(self, text, cwd, rule='main-checkout'):
        d = decide(ps(text, cwd, sub=True), self.w.ctx())
        self.assertIsNotNone(d, 'allowed: %s' % text)
        self.assertEqual(d.rule, rule, d.reason)

    def test_file_paths(self):
        m = self.w.main
        for text, cwd in (('New-Item -Path %s\\x.txt' % m, self.w.wt),
                          ('New-Item -Path %s\\x.txt -Value x' % m, self.w.main),
                          ('New-Item -Path %s -Name x.txt' % m, self.w.wt),
                          ('Set-Content -Path %s\\x.txt -Value 1' % m, self.w.wt),
                          ('New-Item -Path x.txt', self.w.main),
                          ('New-Item -Path .\\env\\x.txt', self.w.main)):
            with self.subTest(text, cwd=cwd):
                self.refused(text, cwd)
        # a drive or provider the guard cannot place: unplaced
        for text in ('New-Item -Path envx:\\x.txt', 'New-Item -Path C:x.txt', 'New-Item -Path Temp:\\x.txt',
                     'New-Item -Path FileSystem::x.txt'):
            with self.subTest(text):
                self.refused(text, self.w.main, 'unplaced')

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
                self.refused(text, self.w.main, 'unplaced')


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
                self.assertEqual(d.rule, 'unplaced', d.reason)

    def test_native_writer_outside_the_roots(self):
        # a provider word the guard cannot place, run in a folder outside every repository: judged
        # as a bash target it cannot place there is, so it passes (docs/guard-v2.md B16, cut)
        text = 'cd "%s"; xcopy a env:FOO' % W.fwd(self.w.plain)
        self.assertIsNone(self.decide(text, self.w.wt), text)


class NarrowSkip(unittest.TestCase):
    """Only the provider-aware item and content cmdlets skip, and only on a line of plain calls:
    a script function (Compress-Archive takes the folder of env:FOO.zip as '.'), the file-only
    writers, and a line with an expression or member call are judged as files."""

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def test_file_only_writers_stay_judged(self):
        for text in ('Compress-Archive -Path README.md -DestinationPath env:FOO.zip',
                     'Out-File -FilePath env:x',
                     '"x" | Out-File env:x',
                     'Invoke-WebRequest https://example.invalid/x -OutFile env:x',
                     'Invoke-RestMethod https://example.invalid/x -OutFile env:x',
                     '"x" | Tee-Object -FilePath env:x',
                     'Get-Process | Export-Csv env:x',
                     'Get-Process | Export-Clixml env:x',
                     'Start-Transcript -Path env:x',
                     'mkdir env:x'):
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed: %s' % text)
                self.assertEqual(d.rule, 'unplaced', d.reason)

    def test_lines_that_are_not_plain_calls_stay_judged(self):
        item = 'New-Item env:\\x.txt'
        for pre in ("$d=$ExecutionContext.('Session'+'State').('Dri'+'ve'); $d.Remove('env', $true, 'local'); "
                    "$d.New($p, 'local')",
                    "$s = $ExecutionContext.SessionState",
                    "$x = 1",
                    "Write-Output $x.Remove()",
                    "[IO.Directory]::CreateDirectory('x')",
                    "$h = $Host",
                    "$o = New-Object -TypeName System.Object",
                    "Add-Type -TypeDefinition $t",
                    "using namespace System.IO",
                    "Get-Process",
                    "& git status",
                    "$g = 'git'; & $g status",
                    "git -C $p status",
                    "Write-Output \"$env:TEMP$(Remove-PSDrive env)\"",
                    "Write-Output $env:TEMP.Length",
                    "where.exe git"):
            for text in ('%s; %s' % (pre, item), '%s\n%s' % (item, pre)):
                with self.subTest(text):
                    d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                    self.assertIsNotNone(d, 'allowed: %s' % text)
                    self.assertIn(d.rule, ('unplaced', 'main-checkout', 'unreadable'), d.reason)


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
            # before the item: refused (a computed command name meets `unreadable` first)
            text = '%s%s%s' % (pre, '\n' if '\n' in pre else '; ', item)
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed: %s' % text)
                self.assertIn(d.rule, ('unplaced', 'main-checkout', 'unreadable'), d.reason)
            # after the item: the line check alone keeps env:\x.txt judged as a file
            text = '%s\n%s' % (item, pre)
            with self.subTest(text):
                d = decide(ps(text, self.w.main, sub=True), self.w.ctx())
                self.assertIsNotNone(d, 'allowed: %s' % text)
                self.assertEqual(d.rule, 'unplaced', d.reason)


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
