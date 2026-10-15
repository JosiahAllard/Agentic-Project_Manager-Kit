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

    def test_called_later_from_a_worktree(self):
        self.allowed(self.run_ps(self.NOT_CALLED_HERE[0]))
        self.allowed(self.run_ps(self.NOT_CALLED_HERE[1]))

    def test_a_jailed_session_that_moves_outside_its_folders(self):
        away = 'Set-Location "%s"; ' % W.fwd(self.w.home)
        for text in self.NOT_CALLED_HERE + self.MISSED:
            with self.subTest(text=text):
                self.denied(self.run_ps(away + text, env=COORDINATOR, sub=False), 'write-jail')


class MoreWriters(Base):
    """OpenWrite, Open and a relative Move destination are writes on a path the guard cannot place."""

    def test_from_a_worktree(self):
        for text in ("[IO.File]::OpenWrite('x.txt')", "[IO.File]::Open('x.txt', 'Create')",
                     r"[IO.File]::Move('{wt}\a.txt', 'b.txt')"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'main-checkout')


# Calls whose arguments the guard's parse loses its place in, so it would read a decoy literal
# ('{wt}\ok') where .NET writes the main checkout: a " inside $(...) in a double-quoted string, a
# smart quote that PowerShell closes with a plain ', and a here-string.
DESYNC = ('[IO.File]::Copy("{wt}\\s.txt$(if(0){{", \'{wt}\\ok\')"}})", \'{main}\\x\')',
          '[IO.File]::Copy(‘{wt}\\s.txt\' + \', "{wt}\\ok")\'.Substring(0,0), \'{main}\\x\')',
          '[IO.File]::Copy(@"\nx", \'{wt}\\ok\') + ("\n"@, \'{main}\\x\')')


class LostPlace(Base):
    """A call whose argument parse may have lost its place is also judged where the line runs."""

    def test_a_subagent_in_the_main_checkout(self):
        for text in DESYNC:
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')

    def test_a_comment_and_a_braced_variable(self):
        for text in ('[IO.File]::Copy(\'{wt}\\s.txt\' <#, \'{wt}\\ok\'\n, #> \'{main}\\x\')',
                     '[IO.File]::Copy("{wt}\\s.txt${{a", \'{wt}\\ok\', "}}", \'{main}\\x\')',
                     # no " at all: PowerShell reads ${a'} and ${b'} as variables, so it copies
                     # to '{main}\x'; a ' inside them opens a string to the parse
                     "[IO.File]::Copy(${{a'}}, '{main}\\x' + ${{b'}}, '{wt}\\ok')"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')


class ParseEdges(Base):
    """_net_lost reads the raw argument text, so a ${ inside a single-quoted string or after a
    backtick counts too: the call is also judged where the line runs (fails closed), and the
    literals it read are still judged. A second call on the line is read on its own."""

    def test_a_braced_variable_in_a_single_quoted_string(self):
        self.denied(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{main}\\x')"), 'main-checkout')
        self.allowed(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{wt}\\x')"))
        self.denied(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{wt}\\x')", cwd=self.w.main), 'main-checkout')

    def test_a_braced_variable_after_a_backtick(self):
        self.denied(self.run_ps("[IO.File]::Copy(\"{wt}\\s`${{a\", '{main}\\x')"), 'main-checkout')

    def test_beside_a_second_call(self):
        self.denied(self.run_ps("[IO.File]::Copy(${{a'}}, '{wt}\\ok') + [IO.File]::Delete('{main}\\x')"),
                    'main-checkout')


class StartDirectory(Base):
    """A .NET write the guard does not read the path of, or cannot place, is also judged in the
    directory the call's process started in (rules.dotnet_process_dir): .NET resolves a relative
    path there, and a Set-Location into a worktree does not move it. Allowed before."""

    def test_set_location_from_the_main_checkout(self):
        for text in Unread.NOT_CALLED_HERE + Unread.MISSED:
            with self.subTest(text=text):
                d = self.denied(self.run_ps("Set-Location '{wt}'; " + text, cwd=self.w.main), 'main-checkout')
                self.assertIn('the directory its process started in', d.reason)

    def test_a_placed_write_is_judged_where_it_points(self):
        self.allowed(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText('{wt}\\x.txt', 'y')",
                                 cwd=self.w.main))

    def test_a_nested_shell_judges_each_start(self):
        nested = "pwsh -NoProfile -Command \"[IO.File]::WriteAllText.Invoke('x.txt', 'y')\""
        self.allowed(self.run_ps(nested))
        self.denied(self.run_ps("Set-Location '{wt}'; " + nested, cwd=self.w.main), 'main-checkout')

    def test_the_owners_own_session_is_not_judged(self):
        self.allowed(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                                 cwd=self.w.main, sub=False))

    def test_a_session_in_the_main_checkout_is_not_offered_a_new_worktree(self):
        # its commands start in the main checkout whatever worktree it makes; offered a new one before
        d = self.denied(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                                    cwd=self.w.main), 'main-checkout')
        self.assertNotIn('worktree add', d.instead)
        self.assertIn('literal absolute paths', d.reason.split('this role may not edit a main checkout.')[1])
        self.assertEqual(d.instead, 'pwd')

    def test_an_outer_location_between_the_event_and_the_call(self):
        # the nested shell starts in the main checkout, where the outer Set-Location put it; the event
        # and the call's own location are the worktree
        self.denied(self.run_ps("Set-Location '{main}'; pwsh -NoProfile -Command \"Set-Location '{wt}'; "
                                "[IO.File]::WriteAllText.Invoke('x.txt', 'y')\""), 'main-checkout')
        self.allowed(self.run_ps("pwsh -NoProfile -Command \"Set-Location '{wt}'; "
                                 "[IO.File]::WriteAllText.Invoke('x.txt', 'y')\""))


class DevicePaths(Base):
    """\\\\?\\C:\\x and \\\\.\\C:\\x are C:\\x to .NET, judged as that path; a share (\\\\host\\C$\\x)
    is a path the guard cannot place. Both were judged as placed, and passed the main-checkout
    rule, before."""

    def share(self, path):
        return '\\\\localhost\\%s$%s' % (path[0], path[2:])

    def test_a_device_path(self):
        for text in (r"[IO.File]::WriteAllText('\\?\{main}\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\\.\{main}\x.txt', 'y')",
                     "[IO.File]::WriteAllText('//?/{main}/x.txt', 'y')"):
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('is in the main checkout', d.reason)
                self.allowed(self.run_ps(d.instead))
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('\\?\{wt}\x.txt', 'y')"))

    def test_a_share(self):
        text = "[IO.File]::WriteAllText('%s\\x.txt', 'y')" % self.share(self.w.main)
        d = self.denied(self.run_ps(text), 'main-checkout')
        self.assertIn('share path', d.reason)
        self.denied(self.run_ps(r"[IO.File]::WriteAllText('\\?\UNC\localhost\share\x.txt', 'y')"),
                    'main-checkout')

    def test_other_device_spellings(self):
        # a volume, a GUID volume, the NT object path and a drive-relative device path name no folder
        # the guard can place
        for text in (r"[IO.File]::WriteAllText('\\?\GLOBALROOT\Device\HarddiskVolume3\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\\?\Volume{{0b9c1a2e-0000-0000-0000-100000000000}}\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\??\{main}\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\\?\%s:x.txt', 'y')" % self.w.main[0]):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'main-checkout')


class Constructors(Base):
    """A StreamWriter or FileStream opened on a path ([IO.StreamWriter]::new, New-Object, a cast
    of a string) is a .NET write as a File call is: unjudged before."""

    INTO_MAIN = (r"$s = [IO.StreamWriter]::new('{main}\x.txt')",
                 r"[System.IO.FileStream]::new('{main}\x.txt', 'Create').Close()",
                 r"$s = New-Object IO.StreamWriter('{main}\x.txt')",
                 r"$s = New-Object System.IO.StreamWriter {main}\x.txt -ErrorAction Stop",
                 r"$s = New-Object -TypeName System.IO.FileStream -ArgumentList '{main}\x.txt', 'Create'",
                 r"$s = New-Object -ArgumentList '{main}\x.txt' -TypeName IO.StreamWriter",
                 r"$s = New-Object IO.FileStream -Args @('{main}\x.txt', 'Create')",
                 r"$s = [System.IO.StreamWriter]'{main}\x.txt'",
                 # a cast of a parenthesized string: allowed from a worktree before
                 r"$s = [System.IO.StreamWriter]('{main}\x.txt')",
                 r"$s = [IO.StreamWriter] ( '{main}\x.txt' )",
                 # a FileStream opened to read that deletes the file when closed (FileOptions): allowed before
                 r"$f = [IO.FileStream]::new('{main}\x.txt', 'Open', 'Read', 'None', 4096, 'DeleteOnClose')",
                 r"$f = New-Object IO.FileStream('{main}\x.txt', [IO.FileMode]::Open, [IO.FileAccess]::Read, "
                 r"[IO.FileShare]::None, 4096, [IO.FileOptions]::DeleteOnClose)")

    def test_into_the_main_checkout(self):
        for text in self.INTO_MAIN:
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('is in the main checkout', d.reason)

    def test_into_the_worktree(self):
        for text in self.INTO_MAIN:
            with self.subTest(text=text):
                self.allowed(self.run_ps(text.replace('{main}', '{wt}')))

    def test_a_relative_or_computed_path(self):
        for text in ("$s = [IO.StreamWriter]::new('x.txt')", '$s = New-Object IO.StreamWriter x.txt',
                     "$f = [IO.FileStream]::new($p, 'Open')"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'main-checkout')

    def test_a_cast_of_anything_but_a_plain_string(self):
        # PowerShell converts the operand, whatever it is, by the StreamWriter(string) constructor;
        # a member access binds before the cast. Allowed from a worktree before.
        for text in ('$s = [IO.StreamWriter]$p', '$s = [IO.StreamWriter] $p.FullName',
                     '$s = [IO.StreamWriter]($p)', "$s = [IO.StreamWriter]('a' + $b)",
                     '$s = [IO.StreamWriter]"$d\\x.txt"', '$s = [IO.StreamWriter][string]$p',
                     "$s = [IO.StreamWriter]'{wt}\\x.txt'.Replace('wt', 'x')",
                     "$s = [IO.StreamWriter]('{wt}\\x.txt').Replace('wt', 'x')",
                     "$s = [IO.StreamWriter]'{wt}\\x.txt'[0]",
                     "$s = [IO.StreamWriter]‘x.txt’", "$s = [IO.StreamWriter]('{wt}\\a', '{wt}\\b')"):
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('a path the line computes', d.reason)
        self.allowed(self.run_ps(r"$s = [IO.StreamWriter]'{wt}\x.txt' + 'y'"))     # the cast binds first

    def test_a_cast_of_a_filestream_or_the_type_as_a_value(self):
        # nothing a cast opens, judged where the line runs, as before
        for text in ('$t = [IO.StreamWriter]', '[IO.StreamWriter].Name', 'if ($x -is [IO.StreamWriter]) {{ 1 }}',
                     '$a = @([IO.StreamWriter], [IO.FileStream])', "[IO.FileStream]'x.txt'"):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text))
                self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')

    def test_a_new_object_call_the_guard_does_not_read(self):
        # an unknown parameter, a third positional argument, an expression after the list:
        # judged where the line runs
        for text in (r"$s = New-Object IO.StreamWriter -Foo '{wt}\x.txt'",
                     r"$s = New-Object IO.StreamWriter '{wt}\x.txt' 'y' 'z'",
                     r"$s = New-Object IO.StreamWriter ('{wt}\ok') + 'x'"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')

    def test_a_stream_or_a_read(self):
        for text in ('$s = [IO.StreamWriter]::new($fs)', '$s = New-Object IO.StreamWriter($fs)',
                     '$s = [IO.StreamWriter]::new($ms, [Text.Encoding]::UTF8)', '$s = [IO.StreamWriter]::Null',
                     "$f = [IO.FileStream]::new($p, 'Open', 'Read', 'ReadWrite')",
                     "$f = [IO.FileStream]::new($p, 'Open', 'Read', 'ReadWrite', 4096)",
                     '$f = New-Object IO.FileStream($p, [IO.FileMode]::Open, [IO.FileAccess]::Read)',
                     r"$s = New-Object IO.StreamReader('{main}\x.txt')",
                     r"$s = [IO.StreamReader]::new('{main}\x.txt')"):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text))


class LockedMain(Base):
    """A repository whose roles keep the main session out of its main checkout: a .NET write
    there that the guard cannot place is judged where the line runs, for every session."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo', 'push_block': True},
                              'roles': {'main': {'edit_main_checkout': False, 'why': 'test'}}})
        self.addCleanup(self.w.cleanup)

    def test_a_computed_path(self):
        self.denied(self.run_ps("$p='x'; [IO.File]::WriteAllText($p,'y')", cwd=self.w.main, sub=False),
                    'main-checkout')

    def test_a_lost_place(self):
        for text in DESYNC:
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main, sub=False), 'main-checkout')

    def test_set_location_into_a_worktree(self):
        self.denied(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                                cwd=self.w.main, sub=False), 'main-checkout')

    def test_a_device_path_or_a_share(self):
        share = '\\\\localhost\\%s$%s' % (self.w.main[0], self.w.main[2:])
        for text in (r"[IO.File]::WriteAllText('\\?\{main}\x.txt', 'y')",
                     "[IO.File]::WriteAllText('%s\\x.txt', 'y')" % share):
            for cwd in (self.w.main, self.w.wt):
                with self.subTest(text=text, cwd=cwd):
                    self.denied(self.run_ps(text, cwd=cwd, sub=False), 'main-checkout')


if __name__ == '__main__':
    unittest.main()
