"""PowerShell .NET file calls ([IO.File]::WriteAllText, [IO.Directory]::CreateDirectory and the
like; rules.net_writes, rules.dotnet_unplaced): .NET resolves a relative path against the
process's working directory ([Environment]::CurrentDirectory), which Set-Location does not move,
so from a worktree such a write may land in the main checkout. A path the guard cannot place (a
relative path, a path the line computes) is refused with the call on an absolute path as the
alternative; a literal absolute path is judged as any other write; reads are left alone."""
import unittest

from kitlib.guard import context

from . import world as W
from .claudehost import decide

COORDINATOR = {context.ROLE_ENV: 'coordinator'}     # a jailed main session: it may edit a main checkout


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def run_ps(self, text, cwd=None, env=None, sub=True):
        text = text.format(main=self.w.main, wt=self.w.wt)
        return decide(W.ps(text, cwd or self.w.wt, sub), self.w.ctx(environ=env or {}))

    def denied(self, d, rule):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)


class Relative(Base):
    """A relative path: the guard cannot tell the process's directory, so it refuses and names the
    call on the absolute path the PowerShell location gives. Allowed from a worktree before."""

    def test_write_all_text_from_worktree(self):
        d = self.denied(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')"), 'main-checkout')
        self.assertIn('[Environment]::CurrentDirectory', d.reason)
        self.assertEqual(d.instead, '[IO.File]::WriteAllText("%s/x.txt", \'y\')' % W.fwd(self.w.wt))
        self.allowed(self.run_ps(d.instead))

    def test_after_set_location(self):
        d = self.denied(self.run_ps("Set-Location sub; [IO.File]::AppendAllText('x.txt', 'y')"), 'main-checkout')
        self.assertEqual(d.instead, '[IO.File]::AppendAllText("%s/sub/x.txt", \'y\')' % W.fwd(self.w.wt))

    def test_directory_and_system_namespace_and_cast(self):
        self.denied(self.run_ps("[IO.Directory]::CreateDirectory('d')"), 'main-checkout')
        self.denied(self.run_ps("[void][System.IO.File]::WriteAllBytes('x.bin', $b)"), 'main-checkout')
        self.denied(self.run_ps("$r = [System.IO.Directory]::Delete('d', $true)"), 'main-checkout')

    def test_a_rooted_path_with_no_drive(self):
        self.denied(self.run_ps(r"[IO.File]::WriteAllText('\x.txt', 'y')"), 'main-checkout')

    def test_a_computed_path(self):
        d = self.denied(self.run_ps("$p = 'x.txt'; [IO.File]::WriteAllText($p, 'y')"), 'main-checkout')
        self.assertIn('$p', d.reason)
        d = self.denied(self.run_ps(r"[IO.File]::WriteAllText([IO.Path]::Combine('a', 'x.txt'), 'y')"),
                        'main-checkout')
        self.assertIn('[IO.Path]::Combine', d.reason)

    def test_a_jailed_main_session(self):
        self.denied(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", env=COORDINATOR, sub=False), 'write-jail')

    def test_the_owners_own_session_is_not_judged(self):
        self.allowed(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", sub=False))

    def test_from_the_main_checkout_the_main_checkout_rule_answers(self):
        d = self.denied(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", cwd=self.w.main), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)


class Absolute(Base):
    """A literal absolute path is the file written, judged as any other write: before, the guard
    judged the directory the line ran in instead."""

    def test_into_the_worktree(self):
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('{wt}\x.txt', 'y')"))
        self.allowed(self.run_ps("[IO.File]::WriteAllText(\"{wt}/x.txt\", 'y')"))

    def test_into_the_main_checkout_from_a_worktree(self):
        d = self.denied(self.run_ps(r"[IO.File]::WriteAllText('{main}\x.txt', 'y')"), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_copy_writes_its_destination_only(self):
        self.allowed(self.run_ps(r"[IO.File]::Copy('{main}\a.txt', '{wt}\b.txt', $true)"))
        self.denied(self.run_ps(r"[IO.File]::Copy('{wt}\a.txt', '{main}\b.txt')"), 'main-checkout')
        self.denied(self.run_ps(r"[IO.File]::Copy('{wt}\a.txt', 'b.txt')"), 'main-checkout')

    def test_move_writes_both(self):
        self.denied(self.run_ps(r"[IO.File]::Move('{main}\a.txt', '{wt}\b.txt')"), 'main-checkout')

    def test_replace_with_no_backup(self):
        self.allowed(self.run_ps(r"[IO.File]::Replace('{wt}\a.txt', '{wt}\b.txt', $null)"))


class Reads(Base):
    """Reads stay allowed, relative or not; OpenRead and OpenText were read as writes before."""

    def test_reads_from_a_worktree(self):
        for text in ("[IO.File]::ReadAllText('x.txt')", "[IO.File]::OpenRead('x.txt')",
                     "[IO.File]::OpenText('x.txt')", "[IO.File]::Exists('x.txt')",
                     "[IO.Path]::GetFullPath('x.txt')"):
            self.allowed(self.run_ps(text))

    def test_open_read_in_the_main_checkout(self):
        self.allowed(self.run_ps("[IO.File]::OpenRead('x.txt')", cwd=self.w.main))

    def test_a_call_named_in_a_string(self):
        self.allowed(self.run_ps("Write-Output \"[IO.File]::WriteAllText('x.txt', 'y')\""))

    def test_the_method_without_a_call(self):
        self.allowed(self.run_ps('[IO.File]::WriteAllText'))


class Unread(Base):
    """A .NET write the guard cannot read the path of - a method named with no '(' after it (called
    later through .Invoke or a variable), or a call whose written argument the guard does not find -
    is judged where the line runs, as every .NET call was before; it is never a write of nothing."""

    NOT_CALLED_HERE = ("[IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                       "$m = [IO.File]::WriteAllText; $m.Invoke('x.txt', 'y')",
                       '[IO.File]::WriteAllText')
    MISSED = ("[IO.File]::Copy(\"$('\"')\", '{main}\\x')",)

    def test_from_the_main_checkout(self):
        for text in self.NOT_CALLED_HERE + self.MISSED:
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')
                self.assertIn('is in the main checkout', d.reason)

    def test_a_jailed_session_that_moves_outside_its_folders(self):
        away = 'Set-Location "%s"; ' % W.fwd(self.w.home)
        for text in self.NOT_CALLED_HERE + self.MISSED:
            with self.subTest(text=text):
                self.denied(self.run_ps(away + text, env=COORDINATOR, sub=False), 'write-jail')


if __name__ == '__main__':
    unittest.main()
