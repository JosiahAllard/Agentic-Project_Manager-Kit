"""The -WorkingDirectory of a shell Start-Process starts (commands.sp_workdir_unplaceable,
commands._sp_shell_text): the nested line runs there only when the text names one folder exactly.
A bare drive, a wildcard, a drive-relative C:x and the other forms the guard cannot place give the
nested line no directory, so a write in it is refused; a literal absolute or relative folder is
still read where it is (docs/guard.md "Parsing", and "Rules" for main-checkout)."""
import unittest

from kitlib.guard import commands

from . import world as W
from .claudehost import decide

BS = chr(92)


class Placeable(unittest.TestCase):
    def test_forms_it_cannot_place(self):
        for text in ('', 'Q:', 'q:', 'Q:x', 'Q:repo' + BS + 'sub', 'Q:' + BS + 'repo?', 'Q:' + BS + 'r*',
                     'Q:' + BS + '[r]epo', 'Q:' + BS + 'a`b', '..' + BS + 'rep?', 'rep*',
                     'Main:' + BS + 'x', 'Registry::HKCU' + BS + 'x', 'FileSystem::Q:', 'FileSystem::Q:x',
                     'Q:' + BS + 'x:stream', BS * 2 + '.' + BS + 'Q:' + BS + 'x', '//./Q:/x',
                     BS * 2 + '?' + BS + 'Q:' + BS + 'x', '/c', '/z/repo', '/Z'):
            with self.subTest(text=text):
                self.assertTrue(commands.sp_workdir_unplaceable(text))

    def test_forms_it_places(self):
        for text in ('Q:' + BS, 'Q:/', 'Q:' + BS + 'repo', 'q:/repo/sub', 'repo', '.', '..' + BS + 'repo',
                     BS + 'repo', '/repo', '~', '~' + BS + 'x', BS * 2 + 'server' + BS + 'share' + BS + 'x',
                     'FileSystem::Q:' + BS + 'repo', 'Microsoft.PowerShell.Core' + BS + 'FileSystem::Q:' + BS + 'r',
                     BS + 'FileSystem::Q:' + BS + 'repo'):
            with self.subTest(text=text):
                self.assertFalse(commands.sp_workdir_unplaceable(text))


class LockedMain(unittest.TestCase):
    """A repository whose roles keep the main session out of its main checkout and no write jail
    (a plain main session): the place the nested line runs is all that stands between its write
    and the main checkout. Before, a bare drive read as the drive's root and a wildcard as a
    folder that does not exist, both outside any checkout, so the write was allowed; and a write
    the guard could not place was judged by the kit's default roles, which let main edit."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo', 'push_block': True},
                              'roles': {'main': {'edit_main_checkout': False, 'why': 'test'}}})
        self.addCleanup(self.w.cleanup)

    def run_ps(self, text, cwd=None):
        return decide(W.ps(text, cwd or self.w.main), self.w.ctx())

    def sp(self, workdir, prog='cmd', args="'/c','echo x > f.txt'"):
        return "Start-Process %s -WorkingDirectory '%s' -ArgumentList %s" % (prog, workdir, args)

    def denied(self, d, rule):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)

    def test_start_process_git_dash_name_workdir(self):
        # a git-* program (git-archive, git-sh-setup) gets no write targets of its own from its
        # arguments; its -RedirectStandard* files are read from the command's directory, the main
        # checkout here, whatever folder -WorkingDirectory names (rules.shell_writes)
        for prog, args in (('git-archive', "'-o','x.zip','HEAD'"), ('git-sh-setup', "'x'"), ('git', "'status'")):
            for workdir in (self.w.wt, self.w.main):
                text = self.sp(workdir, prog, args) + ' -RedirectStandardOutput o.txt'
                with self.subTest(text):
                    self.assertIn('o.txt', self.denied(self.run_ps(text), 'main-checkout').reason)
                # from a worktree the same file is the worktree's
                self.allowed(self.run_ps(self.sp(workdir, prog, args) + ' -RedirectStandardOutput o.txt',
                                         cwd=self.w.wt))

    def test_the_write_without_start_process_is_refused(self):
        # the baseline every row below must keep: this role may not write in main
        self.denied(self.run_ps('cmd /c "echo x > f.txt"'), 'main-checkout')

    def test_a_bare_drive_is_no_place(self):
        drive = self.w.main[:2]
        d = self.denied(self.run_ps(self.sp(drive)), 'unplaced')
        self.assertIn('the directory it runs in is unknown', d.reason)
        self.denied(self.run_ps("Start-Process cmd -WorkingDirectory:%s '/c','echo x > f.txt'" % drive),
                    'unplaced')
        self.denied(self.run_ps("Start-Process cmd -WorkingDirectory: %s '/c','echo x > f.txt'" % drive),
                    'unplaced')

    def test_a_computed_folder_is_no_place(self):
        # read as a literal folder the guard cannot place is: the nested line has no directory
        for workdir in ('$x', '"$x"', '(Join-Path $a b)'):
            text = "Start-Process cmd -WorkingDirectory %s -ArgumentList '/c','echo x > f.txt'" % workdir
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'unplaced')
                self.assertIn('the directory it runs in is unknown', d.reason)
        d = self.denied(self.run_ps("Start-Process powershell -WorkingDirectory $x -ArgumentList "
                                    "'-Command','Set-Content x.txt y'"), 'unplaced')
        self.assertIn('the directory it runs in is unknown', d.reason)

    def test_a_wildcard_is_no_place(self):
        for workdir in (self.w.main[:-1] + '?', self.w.main[:-1] + '*', self.w.main[:-4] + '[r]epo',
                        '..' + BS + 'rep?', self.w.main[:-1] + '`o'):
            with self.subTest(workdir=workdir):
                d = self.denied(self.run_ps(self.sp(workdir)), 'unplaced')
                self.assertIn('the directory it runs in is unknown', d.reason)

    def test_a_wildcard_for_powershell_and_bash(self):
        wild = self.w.main[:-1] + '?'
        self.denied(self.run_ps(self.sp(wild, 'powershell', "'-Command', 'echo x > f.txt'")), 'unplaced')
        self.denied(self.run_ps(self.sp(wild, 'bash', "'-c', '\"echo x > f.txt\"'")), 'unplaced')

    def test_a_drive_relative_folder_is_no_place(self):
        self.denied(self.run_ps(self.sp(self.w.main[:2] + 'repo')), 'unplaced')

    def test_a_relative_folder_is_read_where_it_is(self):
        self.denied(self.run_ps(self.sp('..' + BS + 'repo')), 'main-checkout')
        self.allowed(self.run_ps(self.sp(BS.join(('.claude', 'worktrees', 'wt')))))

    def test_a_target_it_cannot_place_is_judged_by_this_repository(self):
        # rules.main_checkout: with no target to read a .kit.json from, the roles are the
        # repository's the call runs in, not the kit defaults that let main edit anywhere
        for text in ('echo x > $p', 'Set-Content -Path $p -Value x', 'Set-Content -Path (Join-Path $a b) -Value x'):
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'unplaced')
                self.assertIn('cannot tell where it writes', d.reason)
        self.denied(self.run_ps('echo x > $p', cwd=self.w.wt), 'unplaced')

    def test_a_literal_folder_is_read_where_it_is(self):
        self.allowed(self.run_ps(self.sp(self.w.wt)))
        self.denied(self.run_ps(self.sp(self.w.main), cwd=self.w.wt), 'main-checkout')


class GrantedLander(unittest.TestCase):
    """A repository whose .kit.json lets its lander edit the main checkout. A write the guard
    cannot place is still refused to it: the repository grants an edit of its own main checkout,
    and a write with no target may land in any checkout (rules.main_checkout needs the kit
    defaults to allow it too)."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo', 'push_block': True},
                              'roles': {'lander': {'edit_main_checkout': True, 'why': 'test'}}})
        self.addCleanup(self.w.cleanup)

    def judge(self, tool, text, cwd):
        return decide(W.event(tool, {'command': text, 'description': 'test'}, cwd, sub=True,
                              agent_type='lander'), self.w.ctx())

    def denied(self, d, rule='unplaced'):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def test_a_placed_write_in_main_is_still_its_own(self):
        # the grant itself: a literal path in the main checkout, from the main checkout
        self.assertIsNone(self.judge('PowerShell', "Set-Content -Path '%s' -Value x" % BS.join((self.w.main, 'a.md')),
                                   self.w.main))

    def test_a_write_it_cannot_place_is_refused(self):
        for cwd in (self.w.main, self.w.wt):
            with self.subTest(cwd=cwd):
                self.denied(self.judge('Bash', 'cd "$D" && echo x > f.txt', cwd))
                self.denied(self.judge('PowerShell', 'echo x > $p', cwd))

    def test_a_start_process_workdir_it_cannot_place_is_refused(self):
        for cwd in (self.w.main, self.w.wt):
            for workdir in (self.w.main[:2], self.w.main[:-1] + '?'):
                with self.subTest(cwd=cwd, workdir=workdir):
                    self.denied(self.judge('PowerShell', "Start-Process cmd -WorkingDirectory '%s' -ArgumentList "
                                         "'/c','echo x > f.txt'" % workdir, cwd))


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
