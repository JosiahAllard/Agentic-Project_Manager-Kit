"""An agent session's PowerShell .NET write to a relative path (rules.net_process_dir): .NET reads
it against the process's working directory, which cd and Set-Location do not move, so the guard
cannot tell where it lands. A docs-writer in its worktree ran
[IO.File]::WriteAllText('docs/ROSTER.md', ...) and wrote the main checkout. Refused under
write-jail with the same call at the absolute path the PowerShell location gives; a computed path
is judged as a cmdlet's computed target is (unplaced); a literal absolute path as before."""
import unittest
from unittest import mock

from kitlib import guard
from kitlib.guard import context, modes, rules

from . import world as W
from .claudehost import action, decide

COORDINATOR = {context.ROLE_ENV: 'coordinator'}     # a jailed main session
OBSERVED = "[IO.File]::WriteAllText('docs/ROSTER.md', $text)"


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def text(self, t):
        return t.format(main=self.w.main, wt=self.w.wt)

    def run_ps(self, t, cwd=None, env=None, sub=True, agent_type='senior-worker'):
        payload = W.event('PowerShell', {'command': self.text(t), 'description': 'test'},
                          cwd or self.w.wt, sub, agent_type=agent_type)
        return decide(payload, self.w.ctx(environ=env or {}))

    def denied(self, d, rule):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)


class Relative(Base):
    RELATIVE = ("[IO.File]::WriteAllText('x.txt', 'y')", "[IO.File]::WriteAllLines('x.txt', $lines)",
                "[IO.File]::WriteAllBytes('x.bin', $b)", "[IO.File]::AppendAllText('x.txt', 'y')",
                "[IO.File]::Create('x.txt').Close()", r"[IO.File]::Copy('{wt}\a.txt', 'b.txt')",
                r"[IO.File]::Move('{wt}\a.txt', 'b.txt')", "[IO.File]::Delete('x.txt')",
                "[IO.Directory]::CreateDirectory('d')", "[System.IO.Directory]::Delete('d', $true)",
                "[System.IO.File]::WriteAllText('sub/x.txt', 'y')", "$s = [IO.StreamWriter]::new('x.txt')")

    def test_the_observed_form_is_refused(self):
        d = self.denied(self.run_ps(OBSERVED, agent_type='docs-writer'), 'write-jail')
        self.assertIn('[Environment]::CurrentDirectory', d.reason)
        want = '[IO.File]::WriteAllText("%s/docs/ROSTER.md", $text)' % W.fwd(self.w.wt)
        self.assertEqual(d.instead, want)
        self.allowed(self.run_ps(d.instead, agent_type='docs-writer'))

    def test_the_observed_form_after_set_location_from_the_main_checkout(self):
        d = self.denied(self.run_ps("Set-Location '{wt}'; " + OBSERVED, cwd=self.w.main), 'write-jail')
        self.assertIn(W.fwd(self.w.wt) + '/docs/ROSTER.md', d.instead)

    def test_each_write_method_from_a_worktree(self):
        for t in self.RELATIVE:
            with self.subTest(text=t):
                d = self.denied(self.run_ps(t), 'write-jail')
                self.allowed(self.run_ps(d.instead))
                self.denied(self.run_ps(t, env=COORDINATOR, sub=False), 'write-jail')

    def test_two_path_calls_make_every_relative_path_absolute(self):
        # one refusal; its alternative names no relative path, so .NET reads none from the process's directory
        wt = W.fwd(self.w.wt)
        for t, want in (("[IO.File]::Copy('a.txt', 'b.txt')", '[IO.File]::Copy("{0}/a.txt", "{0}/b.txt")'),
                        ("[IO.File]::Move('a.txt', 'b.txt')", '[IO.File]::Move("{0}/a.txt", "{0}/b.txt")'),
                        ("[IO.File]::Replace('a.txt', 'b.txt', 'c.bak')",
                         '[IO.File]::Replace("{0}/a.txt", "{0}/b.txt", "{0}/c.bak")'),
                        ("[IO.File]::Replace('a.txt', 'b.txt', $null)",
                         '[IO.File]::Replace("{0}/a.txt", "{0}/b.txt", $null)')):
            with self.subTest(text=t):
                d = self.denied(self.run_ps(t), 'write-jail')
                self.assertEqual(d.instead, want.format(wt))
                self.allowed(self.run_ps(d.instead))

    def test_move_takes_one_refusal(self):
        d = self.denied(self.run_ps("[IO.File]::Move('a.txt', 'sub/b.txt')"), 'write-jail')
        self.assertIn('a.txt, sub/b.txt', d.reason)
        self.allowed(self.run_ps(d.instead))

    def test_a_double_quoted_relative_path(self):
        d = self.denied(self.run_ps('[IO.File]::WriteAllText("docs/ROSTER.md", $text)'), 'write-jail')
        self.assertEqual(d.instead, '[IO.File]::WriteAllText("%s/docs/ROSTER.md", $text)' % W.fwd(self.w.wt))
        self.allowed(self.run_ps(d.instead))

    def test_new_object_streamwriter(self):
        for t in ("$s = New-Object IO.StreamWriter('x.txt')",
                  "$s = New-Object -TypeName System.IO.StreamWriter -ArgumentList 'x.txt'",
                  "$s = New-Object System.IO.StreamWriter 'x.txt', $true"):
            with self.subTest(text=t):
                d = self.denied(self.run_ps(t), 'write-jail')
                self.assertIn(W.fwd(self.w.wt) + '/x.txt', d.instead)
                self.allowed(self.run_ps(d.instead))

    def test_a_relative_path_into_the_main_checkout_keeps_its_rule(self):
        self.denied(self.run_ps(OBSERVED, cwd=self.w.main), 'main-checkout')
        self.denied(self.run_ps(r"[IO.File]::WriteAllText('..\..\..\x.txt', 'y')"), 'main-checkout')

    def test_the_owners_own_session_is_not_judged(self):
        self.allowed(self.run_ps(OBSERVED, sub=False))

    def test_reads_stay_allowed(self):
        for t in ("[IO.File]::ReadAllText('docs/ROSTER.md')", "[IO.File]::OpenRead('x.txt')",
                  "[IO.File]::Exists('x.txt')"):
            with self.subTest(text=t):
                self.allowed(self.run_ps(t))


class Absolute(Base):
    def test_absolute_paths_are_judged_as_before(self):
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('{wt}\docs\ROSTER.md', $text)"))
        self.allowed(self.run_ps("[IO.File]::WriteAllText('{wt}/docs/ROSTER.md', $text)", cwd=self.w.main))
        self.denied(self.run_ps(r"[IO.File]::WriteAllText('{main}\docs\ROSTER.md', $text)"), 'main-checkout')


class Computed(Base):
    """A path the line computes may be relative too: judged as a cmdlet's computed target is,
    unplaced for an agent session from every location (W.deny_unplaced holds that rule at deny)."""

    COMPUTED = ("[IO.File]::WriteAllText($path, $text)", "$p = 'x.txt'; [IO.File]::WriteAllText($p, 'y')",
                r"[IO.File]::WriteAllText([IO.Path]::Combine('a', 'x.txt'), 'y')")

    def test_a_variable_path_is_unplaced(self):
        for t in self.COMPUTED:
            with self.subTest(text=t):
                d = self.denied(self.run_ps(t), 'unplaced')
                self.assertIn('the guard cannot tell where it writes', d.reason)
                self.denied(self.run_ps(t, env=COORDINATOR, sub=False), 'unplaced')
                self.denied(self.run_ps(t, cwd=self.w.main), 'unplaced')

    def test_the_owners_own_session_in_a_worktree(self):
        self.allowed(self.run_ps(self.COMPUTED[0], sub=False))

    def test_by_default_a_warning(self):
        with mock.patch.dict(modes.DEFAULTS, {rules.UNPLACED: modes.WARN}):
            d = guard.decide(action(W.ps(self.text(self.COMPUTED[0]), self.w.wt, True)), self.w.ctx(environ={}))
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
        self.assertIn('[Environment]::CurrentDirectory', d.reason)


setUpModule = W.deny_unplaced
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
