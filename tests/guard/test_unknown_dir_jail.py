"""A write with a relative path after a move to a location the line computes (`cd $x`), by a
session the write jail holds whose role may edit a main checkout (a coordinator or pm mark): the
guard cannot tell where it writes, so it is unplaced (a warned allow by default, a deny when the
rule denies), as a computed PowerShell target is (rules.ps_drive_jail); it was a silent allow.
The owner's own session and the `plain` mark are not jailed and pass."""
import unittest
from unittest import mock

from kitlib import guard
from kitlib.guard import context, modes, rules

from . import world as W
from .claudehost import action, decide

PS_FORMS = ("cd $x; [IO.File]::WriteAllText('x.txt', 'y')", 'cd $x; Set-Content x.txt y',
            'cd $x; New-Item x.txt', 'cd $x; Remove-Item x.txt', 'Set-Location $x; Set-Content x.txt y')
BASH_FORMS = ('cd $x && echo y > x.txt', 'cd "$x"; touch x.txt', 'cd $x && rm x.txt')
# other moves the guard cannot follow (shell.cd_target gives None): a computed `( )`, pushd, an
# environment variable
PS_MOVES = ('cd (Join-Path $a b); Set-Content x.txt y', 'cd (Get-Item $a).Parent; Set-Content x.txt y',
            'pushd $x; Set-Content x.txt y', 'Push-Location $x; Set-Content x.txt y',
            'cd "$env:X"; Set-Content x.txt y', 'cd $env:X; Set-Content x.txt y')
BASH_MOVES = ('cd "$(dirname "$a")" && echo y > x.txt', 'cd $(pwd)/x && touch x.txt', 'pushd $x && touch x.txt',
              'pushd "$x"; echo y > x.txt', 'cd "$X" && rm x.txt')
# a shell Start-Process starts in a computed -WorkingDirectory: a directory the guard cannot place,
# as a literal bare drive is (commands._sp_shell_text)
SP_FORMS = ("Start-Process cmd -WorkingDirectory $x -ArgumentList '/c','echo x > f.txt'",
            "Start-Process cmd -WorkingDirectory \"$x\" -ArgumentList '/c','echo x > f.txt'",
            "Start-Process cmd -WorkingDirectory (Join-Path $a b) -ArgumentList '/c','echo x > f.txt'",
            "Start-Process powershell -WorkingDirectory $x -ArgumentList '-Command','Set-Content x.txt y'")


def marked(mark):
    return {context.ROLE_ENV: mark} if mark else {}


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def judge(self, form, text, cwd, mark, sub=False):
        ev = (W.ps if form == 'ps' else W.bash)(text, cwd, sub)
        return decide(ev, self.w.ctx(environ=marked(mark)))

    def places(self):
        return (('wt', self.w.wt), ('main', self.w.main))

    def rows(self):
        return [('ps', t) for t in PS_FORMS] + [('bash', t) for t in BASH_FORMS]


class JailedMainSession(Base):
    def test_refused_from_a_worktree_and_from_main(self):
        for mark in ('coordinator', 'pm'):
            for where, cwd in self.places():
                for form, text in self.rows():
                    with self.subTest(mark=mark, where=where, text=text):
                        d = self.judge(form, text, cwd, mark)
                        self.assertIsNotNone(d, 'allowed')
                        self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)
                        self.assertIn('cannot tell where it writes', d.reason)

    def test_a_warned_allow_at_the_default_mode(self):
        with mock.patch.dict(modes.DEFAULTS, {rules.UNPLACED: modes.WARN}):
            for form, text in self.rows():
                with self.subTest(text=text):
                    ev = (W.ps if form == 'ps' else W.bash)(text, self.w.wt, False)
                    d = guard.decide(action(ev), self.w.ctx(environ=marked('coordinator')))
                    self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                    self.assertIn('cannot tell where it writes', d.reason)

    def test_other_moves_it_cannot_follow(self):
        rows = [('ps', t) for t in PS_MOVES] + [('bash', t) for t in BASH_MOVES]
        for mark in ('coordinator', 'pm'):
            for where, cwd in self.places():
                for form, text in rows:
                    with self.subTest(mark=mark, where=where, text=text):
                        d = self.judge(form, text, cwd, mark)
                        self.assertIsNotNone(d, 'allowed')
                        self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)

    def test_a_computed_start_process_directory(self):
        for mark in ('coordinator', 'pm'):
            for where, cwd in self.places():
                for text in SP_FORMS:
                    with self.subTest(mark=mark, where=where, text=text):
                        d = self.judge('ps', text, cwd, mark)
                        self.assertIsNotNone(d, 'allowed')
                        self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)
                        self.assertIn('the directory it runs in is unknown', d.reason)

    def test_an_absolute_target_after_an_unknown_move_is_judged_where_it_points(self):
        # no false positive: the move is unknown, the target is not
        wt = W.fwd(self.w.wt)
        for where, cwd in self.places():
            for form, text in (('ps', "cd $x; Set-Content '%s/x.txt' y" % wt),
                               ('bash', "cd $x && echo y > '%s/x.txt'" % wt)):
                with self.subTest(where=where, text=text):
                    self.assertIsNone(self.judge(form, text, cwd, 'coordinator'))

    def test_a_literal_location_is_judged_where_it_points(self):
        self.assertIsNone(self.judge('ps', "cd '%s'; Set-Content x.txt y" % W.fwd(self.w.wt), self.w.main,
                                     'coordinator'))
        self.assertIsNone(self.judge('bash', 'cd %s && echo y > x.txt' % W.fwd(self.w.wt), self.w.main,
                                     'coordinator'))


class NotJailed(Base):
    def test_the_owner_and_plain_pass(self):
        for mark in (None, 'plain'):
            for where, cwd in self.places():
                for form, text in self.rows():
                    with self.subTest(mark=mark, where=where, text=text):
                        d = self.judge(form, text, cwd, mark)
                        self.assertIsNone(d, d and d.reason)

    def test_a_computed_start_process_directory_passes(self):
        for mark in (None, 'plain'):
            for where, cwd in self.places():
                for text in SP_FORMS:
                    with self.subTest(mark=mark, where=where, text=text):
                        d = self.judge('ps', text, cwd, mark)
                        self.assertIsNone(d, d and d.reason)


class Subagent(Base):
    def test_unplaced_as_before(self):
        for where, cwd in self.places():
            for form, text in self.rows():
                with self.subTest(where=where, text=text):
                    d = self.judge(form, text, cwd, None, sub=True)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)

    def test_a_computed_start_process_directory_is_unplaced(self):
        for where, cwd in self.places():
            for text in SP_FORMS:
                with self.subTest(where=where, text=text):
                    d = self.judge('ps', text, cwd, None, sub=True)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)
                    self.assertIn('the directory it runs in is unknown', d.reason)


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
