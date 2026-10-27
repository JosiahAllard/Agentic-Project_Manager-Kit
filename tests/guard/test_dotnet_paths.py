"""PowerShell .NET file calls ([IO.File]::WriteAllText, [IO.Directory]::CreateDirectory and the
like; rules.net_writes, rules.write_targets): a literal absolute path is judged where it points,
a relative one where the PowerShell location puts it and, for an agent session, refused as well
(rules.net_process_dir; tests/guard/test_dotnet_relative_write.py), a computed one as a cmdlet's
computed target is, and any other path (driveless, a share, one the guard does not read) is not
placed, so it is refused outside a worktree; reads are left alone."""
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
    """A relative path is judged where the PowerShell location puts it, as a bash one is; .NET reads
    it against the process's directory, which the guard cannot see, so an agent session's is
    refused from a worktree too (rules.net_process_dir), with the absolute path as the alternative."""

    def test_net_relative_path_from_a_worktree_is_refused(self):
        for text in ("[IO.File]::WriteAllText('x.txt', 'y')", "Set-Location sub; [IO.File]::AppendAllText('x.txt', 'y')",
                     "[IO.Directory]::CreateDirectory('d')", "[void][System.IO.File]::WriteAllBytes('x.bin', $b)",
                     "$r = [System.IO.Directory]::Delete('d', $true)"):
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'write-jail')
                self.assertIn(W.fwd(self.w.wt) + '/', d.instead)
                self.allowed(self.run_ps(d.instead))

    def test_net_relative_path_refused_outside_worktree(self):
        d = self.denied(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", cwd=self.w.main), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)
        self.assertIn('WriteAllText(', d.instead)

    def test_driveless_from_a_worktree_passes(self):
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('\x.txt', 'y')"))

    def test_computed_from_a_worktree_is_unplaced(self):
        # as a cmdlet's computed target is, from every location
        for text in ("$p = 'x.txt'; [IO.File]::WriteAllText($p, 'y')",
                     r"[IO.File]::WriteAllText([IO.Path]::Combine('a', 'x.txt'), 'y')"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'unplaced')
                self.allowed(self.run_ps(text, sub=False))

    def test_driveless_or_computed_outside_a_worktree_is_unplaced(self):
        for text in (r"[IO.File]::WriteAllText('\x.txt', 'y')", "$p = 'x.txt'; [IO.File]::WriteAllText($p, 'y')",
                     r"[IO.File]::WriteAllText([IO.Path]::Combine('a', 'x.txt'), 'y')"):
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')
                self.assertIn('[Environment]::CurrentDirectory', d.reason)

    def test_a_jailed_main_session_in_its_worktree(self):
        self.denied(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", env=COORDINATOR, sub=False), 'write-jail')
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('{wt}\x.txt', 'y')", env=COORDINATOR, sub=False))

    def test_the_owners_own_session_is_not_judged(self):
        self.allowed(self.run_ps("[IO.File]::WriteAllText('x.txt', 'y')", sub=False))
        self.allowed(self.run_ps("$p = 'x'; [IO.File]::WriteAllText($p, 'y')", cwd=self.w.main, sub=False))


class Absolute(Base):
    """A literal absolute path is the file written, judged where it points, never by the
    directory the line runs in."""

    def test_into_the_worktree(self):
        self.allowed(self.run_ps(r"[IO.File]::WriteAllText('{wt}\x.txt', 'y')"))
        self.allowed(self.run_ps("[IO.File]::WriteAllText(\"{wt}/x.txt\", 'y')"))

    def test_net_literal_worktree_path_from_main_cwd_passes(self):
        for text in (r"[IO.File]::WriteAllText('{wt}\x.txt', 'y')", r"[IO.Directory]::CreateDirectory('{wt}\d')",
                     r"[IO.File]::Copy('{main}\a.txt', '{wt}\b.txt', $true)",
                     r"$s = [IO.StreamWriter]::new('{wt}\x.txt')"):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text, cwd=self.w.main))

    def test_into_the_main_checkout_from_a_worktree(self):
        d = self.denied(self.run_ps(r"[IO.File]::WriteAllText('{main}\x.txt', 'y')"), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_copy_writes_its_destination_only(self):
        self.allowed(self.run_ps(r"[IO.File]::Copy('{main}\a.txt', '{wt}\b.txt', $true)"))
        self.denied(self.run_ps(r"[IO.File]::Copy('{wt}\a.txt', '{main}\b.txt')"), 'main-checkout')
        self.allowed(self.run_ps(r"[IO.File]::Copy('a.txt', '{wt}\b.txt')"))
        self.denied(self.run_ps(r"[IO.File]::Copy('{wt}\a.txt', 'b.txt')"), 'write-jail')
        self.denied(self.run_ps(r"[IO.File]::Copy('{wt}\a.txt', 'b.txt')", cwd=self.w.main), 'main-checkout')

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
    is judged where the line runs; it is never a write of nothing."""

    NOT_CALLED_HERE = ("[IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                       "$m = [IO.File]::WriteAllText; $m.Invoke('x.txt', 'y')",
                       '[IO.File]::WriteAllText')
    MISSED = ("[IO.File]::Copy(\"$('\"')\", '{main}\\x')",)

    def test_from_the_main_checkout(self):
        for text in self.NOT_CALLED_HERE + self.MISSED:
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')
                self.assertIn('is in the main checkout', d.reason)

    def test_called_later_from_a_worktree(self):
        self.allowed(self.run_ps(self.NOT_CALLED_HERE[0]))
        self.allowed(self.run_ps(self.NOT_CALLED_HERE[1]))

    def test_a_jailed_session_that_moves_outside_its_folders(self):
        away = 'Set-Location "%s"; ' % W.fwd(self.w.home)
        for text in self.NOT_CALLED_HERE + self.MISSED:
            with self.subTest(text=text):
                self.denied(self.run_ps(away + text, env=COORDINATOR, sub=False), 'unplaced')


class MoreWriters(Base):
    """OpenWrite, Open and a relative Move destination are writes, judged where the location puts
    them (and, relative, refused for an agent session: rules.net_process_dir)."""

    TEXTS = ("[IO.File]::OpenWrite('x.txt')", "[IO.File]::Open('x.txt', 'Create')",
             r"[IO.File]::Move('{wt}\a.txt', 'b.txt')")

    def test_from_a_worktree(self):
        for text in self.TEXTS:
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'write-jail')
                self.allowed(self.run_ps(text, sub=False))
                self.allowed(self.run_ps(text.replace("'x.txt'", r"'{wt}\x.txt'").replace("'b.txt'", r"'{wt}\b.txt'")))

    def test_from_the_main_checkout(self):
        for text in self.TEXTS:
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'main-checkout')


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
                self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')

    def test_a_comment_and_a_braced_variable(self):
        for text in ('[IO.File]::Copy(\'{wt}\\s.txt\' <#, \'{wt}\\ok\'\n, #> \'{main}\\x\')',
                     '[IO.File]::Copy("{wt}\\s.txt${{a", \'{wt}\\ok\', "}}", \'{main}\\x\')',
                     # no " at all: PowerShell reads ${a'} and ${b'} as variables, so it copies
                     # to '{main}\x'; a ' inside them opens a string to the parse
                     "[IO.File]::Copy(${{a'}}, '{main}\\x' + ${{b'}}, '{wt}\\ok')"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')


class ParseEdges(Base):
    """_net_lost reads the raw argument text, so a ${ inside a single-quoted string or after a
    backtick counts too: the call is also judged where the line runs (fails closed), and the
    literals it read are still judged. A second call on the line is read on its own."""

    def test_a_braced_variable_in_a_single_quoted_string(self):
        self.denied(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{main}\\x')"), 'main-checkout')
        self.allowed(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{wt}\\x')"))
        self.denied(self.run_ps("[IO.File]::Copy('{wt}\\s${{a', '{wt}\\x')", cwd=self.w.main), 'unplaced')

    def test_a_braced_variable_after_a_backtick(self):
        self.denied(self.run_ps("[IO.File]::Copy(\"{wt}\\s`${{a\", '{main}\\x')"), 'main-checkout')

    def test_beside_a_second_call(self):
        self.denied(self.run_ps("[IO.File]::Copy(${{a'}}, '{wt}\\ok') + [IO.File]::Delete('{main}\\x')"),
                    'main-checkout')


class ProcessDirectory(Base):
    """The directory a .NET call's process started in is not judged (docs/guard-v2.md B17): an
    unread call is judged at the PowerShell location only, so a Set-Location from the main checkout
    into a worktree clears it."""

    def test_set_location_into_a_worktree_from_the_main_checkout(self):
        for text in Unread.NOT_CALLED_HERE + Unread.MISSED:
            with self.subTest(text=text):
                self.allowed(self.run_ps("Set-Location '{wt}'; " + text, cwd=self.w.main))

    def test_a_placed_write_is_judged_where_it_points(self):
        self.allowed(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText('{wt}\\x.txt', 'y')",
                                 cwd=self.w.main))
        self.denied(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText('{main}\\x.txt', 'y')",
                                cwd=self.w.main), 'main-checkout')

    def test_a_nested_shell_is_judged_at_its_location(self):
        nested = "pwsh -NoProfile -Command \"[IO.File]::WriteAllText.Invoke('x.txt', 'y')\""
        self.allowed(self.run_ps(nested))
        self.allowed(self.run_ps("Set-Location '{wt}'; " + nested, cwd=self.w.main))
        self.denied(self.run_ps("Set-Location '{main}'; " + nested), 'unplaced')


class DevicePaths(Base):
    """\\\\?\\C:\\x and \\\\.\\C:\\x are C:\\x to .NET, judged as that path; a share (\\\\host\\C$\\x)
    and the other device spellings are paths the guard cannot place, refused outside a worktree."""

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
        self.allowed(self.run_ps(text))
        d = self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')
        self.assertIn('share', d.reason)
        self.denied(self.run_ps(r"[IO.File]::WriteAllText('\\?\UNC\localhost\share\x.txt', 'y')", cwd=self.w.main),
                    'unplaced')

    def test_other_device_spellings(self):
        # a volume, a GUID volume, the NT object path and a drive-relative device path name no folder
        # the guard can place
        for text in (r"[IO.File]::WriteAllText('\\?\GLOBALROOT\Device\HarddiskVolume3\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\\?\Volume{{0b9c1a2e-0000-0000-0000-100000000000}}\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\??\{main}\x.txt', 'y')",
                     r"[IO.File]::WriteAllText('\\?\%s:x.txt', 'y')" % self.w.main[0]):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text))
                self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')


class Constructors(Base):
    """A StreamWriter or FileStream opened on a path ([IO.StreamWriter]::new, New-Object) is a .NET
    write as a File call is, by its first argument."""

    INTO_MAIN = (r"$s = [IO.StreamWriter]::new('{main}\x.txt')",
                 r"[System.IO.FileStream]::new('{main}\x.txt', 'Create').Close()",
                 r"$s = New-Object IO.StreamWriter('{main}\x.txt')",
                 r"$s = New-Object System.IO.StreamWriter {main}\x.txt -ErrorAction Stop",
                 r"$s = New-Object -TypeName System.IO.FileStream -ArgumentList '{main}\x.txt', 'Create'",
                 r"$s = New-Object -ArgumentList '{main}\x.txt' -TypeName IO.StreamWriter",
                 r"$s = New-Object IO.FileStream -Args @('{main}\x.txt', 'Create')",
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

    def test_net_readonly_filestream_counts_as_write(self):
        # a FileStream opened Open, Read writes nothing, but the guard counts it (docs/guard-v2.md
        # v2:114, stricter): allowed before
        for text in (r"$f = [IO.FileStream]::new('{main}\x.txt', 'Open', 'Read')",
                     r"$f = [IO.FileStream]::new('{main}\x.txt', 'Open', 'Read', 'ReadWrite', 4096)",
                     r"$f = New-Object IO.FileStream('{main}\x.txt', [IO.FileMode]::Open, [IO.FileAccess]::Read)"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'main-checkout')
                self.allowed(self.run_ps(text.replace('{main}', '{wt}')))

    def test_net_constructor_argument_indexed(self):
        # the first argument is the path; an append flag or an encoding after it is not a path
        self.denied(self.run_ps(r"$s = [IO.StreamWriter]::new('{main}\x.txt', $true, [Text.Encoding]::UTF8)"),
                    'main-checkout')
        self.allowed(self.run_ps(r"$s = [IO.StreamWriter]::new('{wt}\x.txt', $true, [Text.Encoding]::UTF8)"))
        self.allowed(self.run_ps(r"$s = New-Object IO.StreamWriter('{wt}\x.txt', $true)", cwd=self.w.main))

    def test_a_relative_or_computed_path(self):
        for text, rule, here in (("$s = [IO.StreamWriter]::new('x.txt')", 'main-checkout', 'write-jail'),
                                 ('$s = New-Object IO.StreamWriter x.txt', 'main-checkout', 'write-jail'),
                                 ("$f = [IO.FileStream]::new($p, 'Open')", 'unplaced', 'unplaced')):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), here)
                self.allowed(self.run_ps(text, sub=False))
                self.denied(self.run_ps(text, cwd=self.w.main), rule)

    def test_a_cast_or_the_type_as_a_value(self):
        # the StreamWriter cast is not read (docs/guard-v2.md B7, an unusual spelling): whatever it
        # opens is judged where the line runs, as a FileStream cast and the type as a value are
        for text in (r"$s = [System.IO.StreamWriter]'{main}\x.txt'", r"$s = [System.IO.StreamWriter]('{main}\x.txt')",
                     r"$s = [IO.StreamWriter] ( '{main}\x.txt' )", '$s = [IO.StreamWriter]$p',
                     "$s = [IO.StreamWriter]('a' + $b)", "$s = [IO.StreamWriter]-join ('{main}\\x', '.txt')",
                     r"$s = [IO.StreamWriter]<#c#>'{main}\x.txt'", "$s = [IO.StreamWriter] `\n'{main}\\x.txt'",
                     '$t = [IO.StreamWriter]', '[IO.StreamWriter].Name', 'if ($x -is [IO.StreamWriter]) {{ 1 }}',
                     '$a = @([IO.StreamWriter], [IO.FileStream])', "[IO.FileStream]'x.txt'",
                     'if ($x -is [IO.StreamWriter] -and $y) {{ 1 }}', '$t = [IO.StreamWriter] \t',
                     "$t = [IO.StreamWriter]\n$u = 'x.txt'", "$t = [IO.StreamWriter]\r\n$u = 'x.txt'",
                     "$t = [IO.StreamWriter] # 'x.txt'", '$t = [IO.StreamWriter] <#c#> -eq $u',
                     "$t = [IO.StreamWriter] `\n-and $y", '$t = [IO.StreamWriter] –and $y',
                     "$t = [IO.StreamWriter] -f 'x.txt'", '$t = [IO.StreamWriter]..5',
                     '$l = [Collections.Generic.List[IO.StreamWriter]]::new()'):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text))
                self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')

    def test_a_new_object_call_the_guard_does_not_read(self):
        # an unknown parameter, a third positional argument, an expression after the list:
        # judged where the line runs
        for text in (r"$s = New-Object IO.StreamWriter -Foo '{wt}\x.txt'",
                     r"$s = New-Object IO.StreamWriter '{wt}\x.txt' 'y' 'z'",
                     r"$s = New-Object IO.StreamWriter ('{wt}\ok') + 'x'"):
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main), 'unplaced')

    def test_a_stream_or_a_read(self):
        for text in ('$s = [IO.StreamWriter]::new($fs)', '$s = New-Object IO.StreamWriter($fs)',
                     '$s = [IO.StreamWriter]::new($ms, [Text.Encoding]::UTF8)', '$s = [IO.StreamWriter]::Null',
                     r"$s = New-Object IO.StreamReader('{main}\x.txt')",
                     r"$s = [IO.StreamReader]::new('{main}\x.txt')"):
            with self.subTest(text=text):
                self.allowed(self.run_ps(text))

    def test_a_read_only_filestream_on_a_computed_path(self):
        # a FileStream counts as a write; a computed path is judged as a cmdlet's computed target is
        for text in ("$f = [IO.FileStream]::new($p, 'Open', 'Read', 'ReadWrite')",
                     "$f = [IO.FileStream]::new($p, 'Open', 'Read', 'ReadWrite', 4096)",
                     '$f = New-Object IO.FileStream($p, [IO.FileMode]::Open, [IO.FileAccess]::Read)'):
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'unplaced')
                self.allowed(self.run_ps(text, sub=False))


class LockedMain(Base):
    """A repository whose roles keep the main session out of its main checkout: a .NET write
    there that the guard cannot place is judged where the line runs, for every session."""

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo', 'push_block': True},
                              'roles': {'main': {'edit_main_checkout': False, 'why': 'test'}}})
        self.addCleanup(self.w.cleanup)

    def test_a_computed_path(self):
        self.denied(self.run_ps("$p='x'; [IO.File]::WriteAllText($p,'y')", cwd=self.w.main, sub=False),
                    'unplaced')

    def test_a_lost_place(self):
        for text in DESYNC:
            with self.subTest(text=text):
                self.denied(self.run_ps(text, cwd=self.w.main, sub=False), 'unplaced')

    def test_set_location_into_a_worktree(self):
        # the process's directory is not judged (B17): refused before
        self.allowed(self.run_ps("Set-Location '{wt}'; [IO.File]::WriteAllText.Invoke('x.txt', 'y')",
                                 cwd=self.w.main, sub=False))

    def test_a_device_path_or_a_share(self):
        share = '\\\\localhost\\%s$%s' % (self.w.main[0], self.w.main[2:])
        # a device path names the main checkout, from anywhere; a share is a path the guard cannot
        # place, refused outside a worktree
        for cwd in (self.w.main, self.w.wt):
            with self.subTest(cwd=cwd):
                self.denied(self.run_ps(r"[IO.File]::WriteAllText('\\?\{main}\x.txt', 'y')", cwd=cwd, sub=False),
                            'main-checkout')
        text = "[IO.File]::WriteAllText('%s\\x.txt', 'y')" % share
        self.denied(self.run_ps(text, cwd=self.w.main, sub=False), 'unplaced')
        self.allowed(self.run_ps(text, cwd=self.w.wt, sub=False))


class LoosenedEdges(Base):
    """Pins on the B17 loosening (a relative .NET path judged where the PowerShell location puts
    it): a path that climbs out of the worktree is judged where it lands, and one written after a
    move to a location the line computes is a write the guard cannot place, never a plain allow."""

    CLIMBS = (r"[IO.File]::WriteAllText('..\..\..\x.txt', 'y')", r"[IO.File]::WriteAllText('../../../x.txt', 'y')",
              r"[IO.File]::WriteAllText('sub\..\..\..\..\x.txt', 'y')", r"[IO.File]::WriteAllText('..\x.txt', 'y')",
              r"[IO.Directory]::CreateDirectory('..\..\..\d')", r"[IO.File]::Copy('a.txt', '..\..\..\b.txt')",
              r"$s = [IO.StreamWriter]::new('..\..\..\x.txt')",
              r"Set-Location ..\..\..; [IO.File]::WriteAllText('x.txt', 'y')")
    UNKNOWN = (r"cd $x; [IO.File]::WriteAllText('x.txt', 'y')", r"Set-Location $x; [IO.File]::WriteAllText('x.txt', 'y')",
               r"Push-Location $x; [IO.File]::WriteAllText('x.txt', 'y')",
               r"cd $x; [IO.File]::WriteAllText('..\..\..\x.txt', 'y')",
               r"cd $x; $s = [IO.StreamWriter]::new('x.txt')", r"cd $x; [IO.File]::WriteAllText.Invoke('x.txt', 'y')")

    def test_climbing_out_of_the_worktree_into_the_main_checkout(self):
        for text in self.CLIMBS:
            with self.subTest(text=text):
                self.denied(self.run_ps(text), 'main-checkout')
                self.denied(self.run_ps(text, env=COORDINATOR, sub=False), 'write-jail')

    def test_climbing_out_of_the_repository(self):
        text = r"[IO.File]::WriteAllText('..\..\..\..\x.txt', 'y')"
        self.denied(self.run_ps(text), 'write-jail')
        self.denied(self.run_ps(text, env=COORDINATOR, sub=False), 'write-jail')

    def test_after_a_move_to_an_unknown_location(self):
        for text in self.UNKNOWN:
            with self.subTest(text=text):
                d = self.denied(self.run_ps(text), 'unplaced')
                if 'Invoke' not in text:
                    self.assertIn('cannot tell where it writes', d.reason)

    def test_after_a_move_to_an_unknown_location_by_default(self):
        """At the rule's default mode (unplaced warns) the call goes ahead with the refusal as a
        warning, as a cmdlet write there does; never a silent allow."""
        from unittest import mock
        from kitlib import guard
        from kitlib.guard import modes, rules
        from .claudehost import action
        with mock.patch.dict(modes.DEFAULTS, {rules.UNPLACED: modes.WARN}):
            for text in self.UNKNOWN + (r"cd $x; Set-Content x.txt y",):
                with self.subTest(text=text):
                    d = guard.decide(action(W.ps(text, self.w.wt, True)), self.w.ctx(environ={}))
                    self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                    self.assertIn('the guard cannot tell where it writes' if 'Invoke' not in text
                                  else 'whose path the guard does not read', d.reason)


setUpModule = W.deny_unplaced           # these pin what the guard cannot place (world.deny_unplaced)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
