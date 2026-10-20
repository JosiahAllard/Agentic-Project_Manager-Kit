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


class Subagent(Base):
    def test_unplaced_as_before(self):
        for where, cwd in self.places():
            for form, text in self.rows():
                with self.subTest(where=where, text=text):
                    d = self.judge(form, text, cwd, None, sub=True)
                    self.assertIsNotNone(d, 'allowed')
                    self.assertEqual((d.verdict, d.rule), ('deny', rules.UNPLACED), d.reason)


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
