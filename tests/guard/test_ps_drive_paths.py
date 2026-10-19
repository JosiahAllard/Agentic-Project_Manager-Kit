"""PowerShell provider and drive paths (shell.resolve_path with shell PS, shell.ps_unplaceable,
rules.shell_writes, rules.ps_drive_jail): from a worktree, a write PowerShell sends into the main
checkout through FileSystem::, a module-qualified FileSystem::, a drive named by anything but one
ASCII letter (New-PSDrive Main FileSystem <main>, 1:, _:) or another provider, or after a cd into
such a place, is refused, never read as a file in the worktree. Bash and cmd words, one-letter
drives and relative paths are read as before."""
import os
import time
import unittest

from kitlib.guard import commands as cm
from kitlib.guard import context
from kitlib.guard import shell as sh

from . import world as W
from .claudehost import decide

COORDINATOR = {context.ROLE_ENV: 'coordinator'}     # a jailed main session: it may edit a main checkout


def missing_drive():
    """A drive letter with no drive behind it on this machine."""
    for letter in 'QRMNOPSTUVWXY':
        if not os.path.isdir(letter + ':\\'):
            return letter
    raise unittest.SkipTest('every drive letter tried is in use')


class Base(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def run_ps(self, text, env=None, sub=True):
        text = text.format(main=self.w.main, wt=self.w.wt)
        return decide(W.ps(text, self.w.wt, sub), self.w.ctx(environ=env or {}))

    def run_bash(self, text):
        text = text.format(main=W.fwd(self.w.main), wt=W.fwd(self.w.wt))
        return decide(W.bash(text, self.w.wt, True), self.w.ctx())

    def denied(self, d, rule):
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d

    def allowed(self, d):
        self.assertIsNone(d, d and d.reason)


class IntoMain(Base):
    """Each spelling, from a worktree, of a PowerShell write that lands in the main checkout or a
    place the guard cannot tell: allowed before the fix (the text was joined to the worktree)."""

    def test_new_item_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item FileSystem::{main}\x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_copy_item_filesystem_provider(self):
        self.denied(self.run_ps(r'Copy-Item a.txt FileSystem::{main}\x.txt'), 'main-checkout')

    def test_redirect_filesystem_provider(self):
        self.denied(self.run_ps(r'echo 1 > FileSystem::{main}\x.txt'), 'main-checkout')

    def test_module_qualified_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item Microsoft.PowerShell.Core\FileSystem::{main}\x.txt'),
                        'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_multi_letter_drive(self):
        d = self.denied(self.run_ps(r'New-PSDrive Main FileSystem {main}; New-Item Main:\x.txt'), 'unplaced')
        self.assertIn('cannot place: Main:\\x.txt', d.reason)

    def test_cd_filesystem_provider_then_relative_write(self):
        d = self.denied(self.run_ps(r'cd FileSystem::{main}; New-Item x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_cd_multi_letter_drive_then_relative_write(self):
        d = self.denied(self.run_ps(r'cd Main:\; New-Item x.txt'), 'unplaced')
        self.assertIn('the directory it runs in is a PowerShell drive', d.reason)

    def test_drive_named_by_anything_but_one_ascii_letter(self):
        # New-PSDrive takes names such as 1, _ or a non-ASCII letter: not a one-letter drive
        for text in (r'New-Item 1:\x.txt', r'New-Item _:\x.txt', 'New-Item é:\\x.txt',
                     r'New-PSDrive 1 FileSystem {main}; New-Item 1:\x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'unplaced')
                self.assertIn('cannot place', d.reason)

    def test_cd_drive_named_by_anything_but_one_ascii_letter_then_write(self):
        for text in (r'cd 1:\; New-Item x.txt', r'cd _:\; New-Item x.txt', 'cd é:\\; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'unplaced')
                self.assertIn('Name a literal path inside your worktree', d.reason)

    def test_literal_path_filesystem_provider(self):
        d = self.denied(self.run_ps(r'New-Item -LiteralPath FileSystem::{main}\x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_set_location_and_push_location_filesystem_provider_then_write(self):
        for text in (r'Set-Location FileSystem::{main}; New-Item x.txt',
                     r'Set-Location -LiteralPath FileSystem::{main}; New-Item x.txt',
                     r'Push-Location FileSystem::{main}; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'main-checkout')
                self.assertIn('is in the main checkout', d.reason)

    def test_jailed_session_cd_into_unplaceable_place_then_write(self):
        # cwd and target are both None after the cd: the jail still refuses (track's cwd_ps_drive)
        for text in (r'cd Main:\; New-Item x.txt', r'cd Registry::HKCU\x; New-Item x',
                     r'Set-Location -Path Main:\; New-Item x.txt', r'Push-Location 1:\; New-Item x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text, env=COORDINATOR, sub=False), 'unplaced')
                self.assertIn('PowerShell drive or provider path', d.reason)

    def test_unknown_provider(self):
        # a provider the guard does not know (a module's own, or FileSystem from another module)
        # may hold files anywhere; the built-in non-file ones (Registry::, Environment::) are
        # left to kit/guard-env-drive's skip and are not pinned here
        for text in (r'New-Item Demo.Provider::{main}\x.txt', r'New-Item Other.Module\FileSystem::{main}\x.txt'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'unplaced')
                self.assertIn('cannot place', d.reason)

    def test_jailed_session_that_may_edit_main_is_refused_by_the_jail(self):
        # a coordinator's main session may edit a main checkout, so main_checkout lets an unplaced
        # target through; the jail cannot judge it and refuses (ps_drive_jail)
        d = self.denied(self.run_ps(r'New-Item Main:\x.txt', env=COORDINATOR, sub=False), 'unplaced')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_root_search_through_filesystem_provider(self):
        drive = os.path.splitdrive(self.w.base)[0]
        self.denied(self.run_ps('Get-ChildItem FileSystem::%s\\ -Recurse' % drive), 'root-search')


class DynamicTargets(Base):
    """A PowerShell write target the guard cannot read as a literal ($p, "...$n...", $env:TEMP, a
    ( ) expression), from a worktree: refused, where before it was skipped and allowed
    (rules.shell_writes). $PWD and a literal rest is read against the tracked location
    (rules.ps_pwd_path); a ( ) target fails closed from a main checkout too (shell._Scan.ps_paren_arg)."""

    def test_variable_target_into_main(self):
        d = self.denied(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x'), 'unplaced')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_jailed_session_variable_target_is_refused_by_the_jail(self):
        self.denied(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x', env=COORDINATOR, sub=False), 'unplaced')

    def test_variable_inside_a_literal_path(self):
        self.denied(self.run_ps(r'Set-Content "{wt}\$n.txt" x'), 'unplaced')

    def test_env_temp_target(self):
        self.denied(self.run_ps(r'Set-Content $env:TEMP\x.txt x'), 'unplaced')

    def test_pwd_and_a_literal_rest_is_read_against_the_location(self):
        self.allowed(self.run_ps(r'Set-Content "$PWD\x.txt" x'))
        self.allowed(self.run_ps(r'Set-Content "${{PWD}}\x.txt" x'))
        self.allowed(self.run_ps(r'Set-Content "$PWD\set\x.txt" x'))
        self.allowed(self.run_ps(r'Copy-Item "${{PWD}}/a.txt" "$PWD\b.txt"'))
        d = self.denied(decide(W.ps(r'Set-Content "$PWD\x.txt" x', self.w.main, True), self.w.ctx()),
                        'main-checkout')
        self.assertIn('is in the main checkout', d.reason)

    def test_parenthesised_target(self):
        for text in (r'Set-Content (Join-Path {main} x.txt) x',
                     r'Set-Content -Path:(Join-Path {main} x.txt) x',
                     r'Remove-Item ("{main}\x.txt")'):
            for cwd in (self.w.wt, self.w.main):
                with self.subTest(text=text, cwd=cwd):
                    ev = W.ps(text.format(main=self.w.main), cwd, True)
                    self.denied(decide(ev, self.w.ctx()), 'unplaced')

    def refused_for_both(self, text, cwd=None):
        """`text` is refused for a worker and for the coordinator's mark, which may edit a main
        checkout (rules.ps_drive_jail), each as unplaced (held at deny in this module) because the
        guard cannot tell where it writes."""
        text = text.format(main=self.w.main, wt=self.w.wt)
        cwd = cwd or self.w.wt
        d = self.denied(decide(W.ps(text, cwd, True), self.w.ctx()), 'unplaced')
        self.assertIn('cannot tell where it writes', d.reason)
        d = self.denied(decide(W.ps(text, cwd, False), self.w.ctx(environ=COORDINATOR)), 'unplaced')
        self.assertIn('cannot tell where it writes', d.reason)

    def test_computed_target_is_judged_from_every_location(self):
        # rules.shell_writes: after a computed cd (the location is unknown) and in the main
        # checkout too, where only main_checkout judged it and let the coordinator's mark through
        for text, cwd in ((r'cd (Get-Location); Set-Content $p x', self.w.wt),
                          (r'$p="{main}\x.txt"; Set-Content $p x', self.w.main),
                          (r'Set-Content (Join-Path {main} x.txt) x', self.w.main)):
            with self.subTest(text=text, cwd=cwd):
                self.refused_for_both(text, cwd)

    def test_a_splat_is_a_computed_target(self):
        # rules.write_targets: @a binds what its table holds, -Path among them
        for text in (r"$a=@{{Path='{main}\x.txt'}}; Set-Content @a hi", r'Set-Content @a hi',
                     r'Copy-Item a.txt @a', r'Remove-Item @a', r'New-Item x.txt @a'):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_pwd_is_read_only_when_the_write_is_the_lines_one_command(self):
        # rules.ps_pwd_path: another command, a pipeline, a script block or an argument that runs
        # code could set PWD first, under any spelling of its name
        for text in (r'$PWD = "{main}"; Set-Content "$PWD\x.txt" x',
                     r'cd {main}; Set-Content "$PWD\x.txt" x',
                     r"Get-Item {main} -OutV ('P'+'WD'); Set-Content " + r'"$PWD\x.txt" x',
                     r"[scriptblock]::Create('sv PWD {main}').Invoke(); Set-Content " + r'"$PWD\x.txt" x',
                     r'Set-Content a.txt x; Set-Content "$PWD\x.txt" x',
                     r'Set-Content "$PWD\x.txt" x; Copy-Item a.txt .',
                     r'Get-Item {main} -pv PWD | % {{ Set-Content "$PWD\x.txt" x }}',
                     r'Get-Item {main} | Set-Content "$PWD\x.txt"',
                     r'foreach ($PWD in "{main}") {{ Set-Content "$PWD\x.txt" x }}',
                     r'& {{ param($PWD) Set-Content "$PWD\x.txt" x }} {main}',
                     r"Set-Content -Value ([scriptblock]::Create('sv PWD {main}').Invoke()) " + r'"$PWD\x.txt"',
                     r'Set-Content -Value "$(sv PWD {main})" -Path "$PWD\x.txt"'):
            with self.subTest(text):
                self.refused_for_both(text)
        # & and a computed program is refused before any write is read (rules.call_subexpr_pmdata: unplaced)
        self.denied(self.run_ps(r"& ([scriptblock]::Create('sv PWD {main}')); Set-Content " + r'"$PWD\x.txt" x'),
                    'unplaced')
        # a line that writes to a literal target is not judged by this rule
        for text in ('Write-Output "a`nb"; Set-Content x.txt "c`nd"', 'sv a 1; Set-Content x.txt $a'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_named_parameter_with_a_computed_value_binds_it(self):
        # rules.ps_params: `-Destination:$p` is that parameter's value, not a positional
        for text in (r'Copy-Item -Destination:$p a.txt', r'Copy-Item -Destination:(Join-Path {main} x) a.txt',
                     r'Compress-Archive -DestinationPath:$p x.zip', r'Expand-Archive x.zip -DestinationPath:$p'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text in (r'Set-Content -Value:$v x.txt', r'Copy-Item -Path:$p x.txt'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_colon_then_a_space_binds_the_next_word(self):
        # rules.ps_params: `-Path: x` is -Path x, not -Path '' and a positional x
        d = self.denied(self.run_ps(r'Set-Content -Path: {main}\x.txt hi'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)
        self.refused_for_both(r'Set-Content -Path: $p hi')
        self.allowed(self.run_ps(r'Set-Content -Path: x.txt hi'))
        self.allowed(self.run_ps(r'Set-Content -Value: {main}\x.txt x.txt'))

    def test_a_paren_argument_after_a_keyword_word_or_touching_a_parameter(self):
        # shell._Scan.ps_paren_arg: the keyword is checked in first position only, and
        # -Destination(...) is that parameter's value
        for text in (r'Copy-Item data (Join-Path {main} x)', r'Copy-Item in (Join-Path {main} x)',
                     r'Copy-Item end (Join-Path {main} x)', r'Copy-Item process (Join-Path {main} x)',
                     r'Copy-Item a.txt -Destination(Join-Path {main} x)'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text, words in (('Copy-Item data (Join-Path a b)', ['Copy-Item', 'data', '(Join-Path a b)']),
                            ('Copy-Item a -Destination(Join-Path a b)',
                             ['Copy-Item', 'a', '-Destination', '(Join-Path a b)'])):
            with self.subTest(text):
                p = cm.read(text, sh.PS, W.fwd(self.w.wt))
                self.assertEqual([c.argv for c in p.commands if c.nested_in is None], [words])
        for text in ('$r = if (Test-Path x) { 1 }', 'if (-not(Test-Path x)) { 1 }'):
            with self.subTest(text):
                p = cm.read(text, sh.PS, W.fwd(self.w.wt))
                self.assertTrue(any(c.argv[:1] == ['Test-Path'] and c.nested_in is None and c.groups
                                    for c in p.commands), [(c.argv, c.groups) for c in p.commands])

    def test_a_paren_touching_a_plain_word_is_an_argument(self):
        # shell._Scan.ps_paren_arg: `func(arg)` passes the word, then the ( ) as the next argument
        for text in (r'Copy-Item a.txt(Join-Path {main} x)', r'Set-Content(Join-Path {main} x.txt) hi'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text, words in (('Copy-Item a.txt(Join-Path a b)', ['Copy-Item', 'a.txt', '(Join-Path a b)']),
                            ('Set-Content(Join-Path a b) hi', ['Set-Content', '(Join-Path a b)', 'hi'])):
            with self.subTest(text):
                p = cm.read(text, sh.PS, W.fwd(self.w.wt))
                self.assertEqual([c.argv for c in p.commands if c.nested_in is None], [words])
        # a method call stays one word: $x.M( ), [T]::M( ), 'lit'.M( ), (expr).M( )
        for text in (r"Set-Content x.txt -Value $s.Replace('a','b')", r"$t = $s.Replace('a','b'); Set-Content x.txt $t",
                     r"Set-Content x.txt -Value ([IO.Path]::Combine('a','b'))", r"Set-Content x.txt -Value 'abc'.Replace('a','b')",
                     r'Set-Content x.txt -Value (Get-Date).AddDays(1)', r'if(Test-Path x) {{ New-Item y.txt }}',
                     r'foreach($f in 1,2) {{ New-Item y.txt }}'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_paren_touching_any_word_but_a_member_is_the_next_argument(self):
        # shell._Scan.ps_member_call: PowerShell reads `'x'(...)`, `$x(...)`, `$x[(...)]`,
        # `[string](...)` and `.\x(...)` as the word, then the ( ) as the next argument; only a
        # member access ($s.M(, 'lit'.M(, (x).M(, [T]::M() calls a method
        for text in (r'Set-Content -Value $x[(1)] -Path $p',
                     r'Set-Content -Value $x[(sv PWD {main})] -Path "$PWD\x.txt"',
                     r"Set-Content -Value 'x'(sv PWD {main}) -Path " + r'"$PWD\x.txt"',
                     r'Set-Content -Value .\x(sv PWD {main}) -Path "$PWD\x.txt"',
                     r'Set-Content -Value $x(sv PWD {main}) -Path "$PWD\x.txt"',
                     r'Set-Content -Value [string](sv PWD {main}) -Path "$PWD\x.txt"',
                     r'Copy-Item .\a.txt(Join-Path {main} x)', r"Copy-Item 'a'(Join-Path {main} x)",
                     r'Copy-Item $a(Join-Path {main} x)', r'Copy-Item $a[0](Join-Path {main} x)',
                     r'Copy-Item [string](Join-Path {main} x)'):
            with self.subTest(text):
                self.refused_for_both(text)
        d = self.denied(self.run_ps(r'Set-Content -Value $x[(1)] -Path {main}\x.txt'), 'main-checkout')
        self.assertIn('is in the main checkout', d.reason)
        p = cm.read("Set-Content -Value 'x'(1) -Path x.txt", sh.PS, W.fwd(self.w.wt))
        self.assertEqual([c.argv for c in p.commands if c.nested_in is None],
                         [['Set-Content', '-Value', 'x', '(1)', '-Path', 'x.txt']])

    def test_a_command_the_line_splits_around_a_group_fails_closed(self):
        # shell.Command.split: a { } or a method call's ( ) the scanner reads as a group, with more
        # of the command's arguments after it: the write's target is unknown (rules.write_targets)
        # and the line unreadable (commands.expand)
        # rules.ps_pwd_path: a $PWD write whose own arguments hold a group the line is cut at is not
        # the line's one command, split or not
        for text in (r'Set-Content -Value {{sv PWD {main}}} "$PWD\x.txt"',
                     r'Set-Content "$PWD\x.txt" -Value {{sv PWD {main}}}', r'Set-Content "$PWD\x.txt" -Value $s.Trim()',
                     r'Set-Content -Value $s.Trim() -Path {main}\x.txt',
                     r'Copy-Item $a.M(1)(Join-Path {main} x)',
                     r'Set-Content -Value hi -Path "{wt}\x.txt".Trim()'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text in (r'Write-Output $s.Trim() x', r'Get-Item a | ForEach-Object -Begin {{ 1 }} -Process {{ 2 }}'):
            with self.subTest(text):
                d = self.denied(self.run_ps(text), 'unreadable')
                self.assertIn('a command the line splits', d.reason)
        p = cm.read('Set-Content -Value $s.Trim() -Path x.txt', sh.PS, W.fwd(self.w.wt))
        self.assertEqual([(c.argv, c.split) for c in p.commands],
                         [(['Set-Content', '-Value', '$s.Trim'], True), (['-Path', 'x.txt'], False)])
        # a keyword's own ( ) and { }, a method chain, a pipeline after a block: no split
        for text in ('(Get-Date).AddDays(1) | Out-File x.txt', "$s.Replace('a','b').Trim()",
                     "[IO.File]::ReadAllText('a').Trim()", 'if ((Test-Path a) -and (Test-Path b)) {{ 1 }} else {{ 2 }}',
                     'try {{ Get-Item a }} catch {{ 1 }} finally {{ 2 }}', 'do {{ 1 }} while ($x)',
                     '$r = if ($a) {{ 1 }} else {{ 2 }}', 'Get-ChildItem | Where-Object {{ $_.Length -gt 1 }} | Select-Object Name',
                     '& Set-Content x.txt y', 'Set-Content x.txt -Value $s.Trim()', 'git status 2>$null'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_group_argument_to_a_writer_is_a_computed_target_wherever_it_sits(self):
        # rules.ps_unread: a { } or a ( ) the scanner reads as a group among a writer's
        # arguments, last on the line too, is a target it cannot tell; a member call is one word
        # not literal, bound as any argument (shell._Scan.ps_paren_arg)
        for text in (r"'x' | Set-Content -Path {{ $p }}", r"'x' | Set-Content {{ $p }}",
                     r'Get-ChildItem *.txt | Copy-Item -Destination {{ "{main}\$($_.Name)" }}',
                     r'robocopy . {{ $p }}', r'Set-Content -Value hi -Path [x]::y($p)',
                     r"Set-Content -Value hi -Path 's'.M($p)", r'Set-Content -Value hi -Path $s.Trim()',
                     r'Set-Content -Value hi -$x(Join-Path {main} x.txt)'):
            with self.subTest(text):
                self.refused_for_both(text)
        p = cm.read("'x' | Set-Content -Path { $p }", sh.PS, W.fwd(self.w.wt))
        self.assertEqual([(c.argv, c.split, c.next_op) for c in p.commands],
                         [(["x"], False, '|'), (['Set-Content', '-Path'], False, '{'), (['$p'], False, '}')])
        # not a writer, or a member call bound to a parameter that is no target: not judged
        for text in ('Get-ChildItem | ForEach-Object {{ $_.Name }}', 'Write-Output $s.Trim()',
                     'Set-Content x.txt -Value $s.Trim()'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_paren_that_does_not_touch_the_word_before_is_an_argument(self):
        # shell._Scan.ps_paren_arg: after a redirection, a line continuation or an inline comment
        for text in (r'Set-Content -Value hi 2>&1 (Join-Path {main} x.txt)',
                     'Set-Content -Value hi -Path `\n(Join-Path {main} x.txt)',
                     'Set-Content -Value hi -Path<#c#>(Join-Path {main} x.txt)',
                     'Set-Content -Value hi -Path <#c#>(Join-Path {main} x.txt)'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text in ('Set-Content -Value hi 2>&1 (Join-Path a b)', 'Set-Content -Value hi <#c#>(Join-Path a b)'):
            with self.subTest(text):
                p = cm.read(text, sh.PS, W.fwd(self.w.wt))
                self.assertEqual([c.argv for c in p.commands if c.nested_in is None],
                                 [['Set-Content', '-Value', 'hi', '(Join-Path a b)']])

    def test_a_splat_to_a_copier_is_a_computed_target(self):
        # rules.write_targets: robocopy and xcopy bind a splat as a cmdlet does
        for text in (r'robocopy . @a', r'robocopy @a', r'xcopy a.txt @a'):
            with self.subTest(text):
                self.refused_for_both(text)
        self.allowed(self.run_ps("robocopy . '@a'"))

    def test_a_redirection_to_a_group_is_a_computed_target(self):
        # shell._Scan.target: PowerShell writes to the value of the ( ) or the text of the { }
        for text in (r"'x' > (Join-Path {main} x.txt)", r"'x' >> (Join-Path {main} x.txt)",
                     r"'x' 2> ($p)", r"'x' > {{ {main}\x.txt }}"):
            with self.subTest(text):
                self.refused_for_both(text)
        p = cm.read("'x' > (Join-Path a b)", sh.PS, W.fwd(self.w.wt))
        self.assertEqual(p.unclean, [])
        self.assertEqual(sorted(c.argv for c in p.commands), [['Join-Path', 'a', 'b'], ['x']])
        self.assertEqual([(r.target.text, r.target.dyn) for c in p.commands for r in c.redirects],
                         [('(Join-Path a b)', True)])

    def test_a_keywords_own_group_marks_no_command(self):
        # shell._Scan.commands: if ( ) { }, $r = if ( ) { }, foreach ( ) { }: the keyword's own
        # groups cut no command
        for text in ('if ($a) { 1 } else { 2 }', '$r = if ($a) { 1 }', 'foreach ($f in $g) { 1 }',
                     'try { 1 } catch [IO.IOException] { 2 }', 'function f($a) { 1 }'):
            with self.subTest(text):
                p = cm.read(text, sh.PS, W.fwd(self.w.wt))
                self.assertEqual([c.argv for c in p.commands if c.split], [])

    def test_a_script_block_to_any_writer_fails_closed(self):
        # rules.ps_unread, as docs/guard.md "Rules" names it: a delay-bind script block, a
        # { } to a parameter that is no target, an Export-* cmdlet, tar from PowerShell; and a
        # ( ) touching a group's ), last on the line
        for text in ("Get-ChildItem *.txt | Rename-Item -NewName {{ $_.Name + '.bak' }}",
                     'Set-Content x.txt -Value {{ 1 }}', 'Export-Csv -Path x.csv -InputObject {{ 1 }}',
                     'tar -xf a.tar -C {{ x }}', r'Copy-Item a.txt $a.M(1)(Join-Path {main} x)'):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_a_quoted_equals_or_keyword_is_no_keyword(self):
        # shell._ps_keyword_led reads only a keyword spelled bare; rules.ps_unread: a word after a
        # comma, and a writer cut at a group its words do not account for
        for text in (r"Set-Content -Value '=','if' -Path (Join-Path {main} x.txt)",
                     r"Set-Content -Value '=' if -Path (Join-Path {main} x.txt)",
                     r"Set-Content -Value 'if' -Path (Join-Path {main} x.txt)"):
            with self.subTest(text):
                self.refused_for_both(text)
        p = cm.read("Set-Content -Value '=','if' -Path (Join-Path a b)", sh.PS, W.fwd(self.w.wt))
        self.assertEqual([c.argv for c in p.commands if c.nested_in is None],
                         [['Set-Content', '-Value', '=', 'if', '-Path', '(Join-Path a b)']])

    def test_a_value_that_is_a_type_members_call_fails_closed(self):
        # rules.ps_unread: -Value's value is passed over only when it is one value that holds no
        # command; `[x]::y(` and `[x].y(` are not (PowerShell passes [x]::y as a word, the ( ) next)
        for text in (r'Set-Content -Value [x]::y(Join-Path {main} z.txt)',
                     r'Set-Content -Value [x].y(Join-Path {main} z.txt)',
                     r"Set-Content x.txt -Value [IO.Path]::Combine('a','b')"):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_a_splat_or_variable_to_a_native_writer_fails_closed(self):
        # rules.ps_unread: PowerShell spreads a splat, and an array in a variable, into a native
        # program's arguments; an Export-* cmdlet binds a splat as any cmdlet does
        for text in (r'tar -xf a.tar @a', r'curl.exe -o x @a', r'wget.exe @a', r'Export-Alias @a',
                     r'robocopy $a x', r'tar -xf a.tar $a', r'curl.exe -o x.txt -H $h', r'Remove-Item a.txt,b.txt'):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_a_redirection_past_a_continuation_or_comment_is_a_computed_target(self):
        # rules.write_targets: a PowerShell redirection whose target is not a plain word, or that
        # has none the scanner read
        for text in ("'x' > `\n(Join-Path {main} x.txt)", "'x' > <#c#>(Join-Path {main} x.txt)",
                     "'x' >> `\n(Join-Path {main} x.txt)"):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_a_group_to_an_export_cmdlet_or_native_downloader_fails_closed(self):
        # rules.ps_unread on the Export-* branch (a cmdlet not in PS_CMDLETS) and on curl and wget,
        # with a group their words do not hold: a block, or a ( ) touching a method call's )
        for text in (r'Export-Alias -Path x.txt -Description {{ 1 }}',
                     r'Export-Alias -Name a $a.M(1)(Join-Path {main} y.txt)',
                     r'curl.exe -o x.txt -H {{ 1 }}', r'curl.exe -o x.txt $a.M(1)(Join-Path {main} y)',
                     r'wget.exe -O x.txt $a.M(1)(Join-Path {main} y)', r'curl -o x.txt $a.M(1)(Join-Path {main} y)'):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_the_coarse_rule_passes_plain_arguments_and_a_value(self):
        # rules.ps_unread: a value param's one value, a plain word, a quoted '@a', a variable a
        # cmdlet binds as one argument
        for text in (r'Set-Content x.txt -Value $s', r'Set-Content x.txt -Value $s.Trim()',
                     r'Set-Content x.txt hi', r"robocopy . '@a'", r'Export-Alias x.txt',
                     r'Set-Content x.txt -Value "a $s b"', r'Out-File -FilePath x.txt -Encoding utf8',
                     r'tar -xf a.tar -C out', r'curl.exe -o x.txt https://example.invalid/a'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_an_export_cmdlet_judges_every_argument_that_could_be_its_path(self):
        # rules.ps_export_targets: an Export-* cmdlet the guard has no spec for binds an unknown
        # param's value and a positional it cannot place as the path, not as Export-Clixml would
        for text in (r'Export-Alias -As Script $p', r'Export-Alias -Name a $p', r'Export-Alias -As:Script $p',
                     r'Export-Counter -FileFormat csv $p', r'Export-Alias -Name a -Path:x.ps1 -Description $p'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text in (r'Export-Alias -As Script {main}\x.ps1', r'Export-Alias -Name a -Path {main}\x.ps1',
                     r'Export-Alias -Description d {main}\x.ps1'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        for text in (r'Export-Alias -As Script x.ps1', r'Export-Alias -Name a x.ps1',
                     r'Export-Clixml -Depth 2 x.xml', r'Export-Alias -Encoding utf8 x.ps1'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_the_built_in_export_aliases_are_the_cmdlets(self):
        # rules.PS_ALIASES: epal is Export-Alias, epsn is Export-PSSession
        for text in (r'epal {main}\x.ps1', r'epsn -OutputModule {main}\m', r'epsn {main}\m'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        for text in (r'epal $p', r'epsn -OutputModule $p'):
            with self.subTest(text):
                self.refused_for_both(text)
        self.allowed(self.run_ps(r'epal x.ps1'))

    def test_a_typographic_dash_names_a_parameter(self):
        # shell.PS_DASHES: PowerShell reads an en dash, an em dash and a horizontal bar as '-'
        # (rules.ps_params, rules.ps_unread, commands.canon_param, the powershell -Command scan)
        for text in ('Copy-Item a.txt –Destination $p', 'Set-Content –Value hi –Path $p',
                     'Out-File ―FilePath $p', 'Remove-Item —LiteralPath $p'):
            with self.subTest(text):
                self.refused_for_both(text)
        for text in ('Copy-Item a.txt —Destination {main}\\x', 'saps python –RedirectStandardOutput {main}\\x.txt',
                     'powershell –Command "Remove-Item {main}\\x.txt"', 'powershell /c "Remove-Item {main}\\x.txt"',
                     'Invoke-Expression –Command "Remove-Item {main}\\x.txt"',
                     'Invoke-Expression –Verbose "Remove-Item {main}\\x.txt"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        self.denied(self.run_ps('Stop-Process –Name python'), 'kill-by-name')
        for text in ('Copy-Item a.txt –Destination b.txt', 'Set-Content –Path x.txt –Value $s',
                     'Invoke-Expression –Verbose "Write-Output hi"'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_an_unknown_powershell_switch_is_judged_both_ways(self):
        # commands._nested_text: powershell.exe runs a switch it does not know, and every word after
        # it, as the command; pwsh reads the later -Command; both readings are judged
        for text in ('powershell "-x; Remove-Item {main}\\x.txt"', 'powershell "–x; Remove-Item {main}\\x.txt"',
                     'powershell "/x; Remove-Item {main}\\x.txt"',
                     'powershell "-x; Remove-Item {main}\\x.txt" -c "Write-Output hi"',
                     'pwsh -Zork -c "Remove-Item {main}\\x.txt"',
                     'pwsh -ConfigurationFile c.json -c "Remove-Item {main}\\x.txt"',
                     # -o Text is -OutputFormat and its value
                     'powershell -o Text -c "Remove-Item {main}\\x.txt"',
                     'powershell -i Text -c "Remove-Item {main}\\x.txt"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        for text in ('powershell -Zork -c $s', 'powershell -Zork -c -'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'unreadable')
        for text in ('powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Write-Output hi"',
                     'pwsh -nop -noni -NoProfileLoadTime -c "Write-Output hi"', 'pwsh -Zork -c "Write-Output hi"'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_switch_the_shells_read_with_and_without_a_value_is_judged_both_ways(self):
        # commands.PS_VALUE_EITHER: -i and -in are -InputFormat with a value to powershell.exe and
        # -Interactive without one to pwsh; an -EncodedArguments prefix takes a value. The word
        # after the switch is read both as that value and as the start of the command, and the
        # scan goes on from each reading
        for text in ('powershell -i Text "Remove-Item {main}\\x.txt"',
                     'powershell -in Text "Remove-Item {main}\\x.txt"',
                     'powershell -i Text -nop "Remove-Item {main}\\x.txt"',
                     'powershell -encodeda x "Remove-Item {main}\\x.txt"',
                     'powershell -EncodedArguments x "Remove-Item {main}\\x.txt"',
                     'powershell -ea x "Remove-Item {main}\\x.txt"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        for text in ('powershell -i Text "Write-Output hi"', 'pwsh -i -nop -c "Write-Output hi"'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))
        # commands.PS_FORKS_MAX: past that many such switches on one reading path the line is not
        # read; the reading that takes no value is still judged
        self.denied(self.run_ps('powershell %s"Write-Output hi"' % ('-i ' * 40)), 'unreadable')
        for shell in ('powershell', 'pwsh'):
            for n in (17, 18, 40):
                text = '%s %s-c "Remove-Item {main}\\x.txt"' % (shell, '-i ' * n)
                with self.subTest(text):
                    self.denied(self.run_ps(text), 'main-checkout')
        # past the cap -i is read with no value, so Text starts the command and Remove-Item is its
        # argument; the reading that takes Text as the value (Remove-Item the command) is not
        # followed, so the line is flagged
        text = 'pwsh %sText "Remove-Item {main}\\x.txt"' % ('-i ' * 40)
        self.denied(self.run_ps(text), 'unreadable')
        self.assertIn('a powershell switch line with too many readings',
                      cm.read(text.format(main=self.w.main), sh.PS, self.w.wt).unclean)

    def test_readings_that_fork_again_at_each_level_are_bounded(self):
        # commands.MAX_NESTS: each reading of `pwsh -i pwsh -i pwsh ...` is a pwsh line that forks
        # again, some 17 readings per level down to shell.MAX_DEPTH (17^6 parses: over 20000 and
        # 25 s before the cap, r11 probe). The line's nested parses stop at the cap and it is flagged
        text = 'pwsh' + ' -i pwsh' * 110 + '; Remove-Item {main}\\x.txt'
        parsed = cm.read(text.format(main=self.w.main), sh.PS, self.w.wt)
        self.assertLessEqual(parsed.nests, cm.MAX_NESTS)
        self.assertIn('more than %d nested shell texts' % cm.MAX_NESTS, parsed.unclean)
        start = time.monotonic()
        d = self.run_ps(text)
        self.assertLess(time.monotonic() - start, 5.0)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual(d.verdict, 'deny', d.reason)
        self.assertIn(d.rule, ('main-checkout', 'unreadable'), d.reason)

    def test_each_reading_of_a_powershell_line_is_parsed_on_its_own(self):
        # commands._nested_text gives each reading as a text of its own and commands.expand parses
        # each: an unclosed <# in one reading hides no other reading, nor standard input
        for text in ('pwsh -CustomPipeName "<#" -c "Remove-Item {main}\\x.txt"',
                     'powershell -CustomPipeName "<#" -c "Remove-Item {main}\\x.txt"',
                     'echo "Remove-Item {main}\\x.txt" | pwsh -CustomPipeName "<#"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')

    def test_the_readings_of_a_switch_line_are_counted_once(self):
        # commands._nested_text: readings are kept once each, so sixteen -i give one copy of the
        # command, not one per reading path (1597), and standard input is read once
        def said(text):
            parsed = cm.read(text, sh.PS, self.w.wt)
            return [c for c in parsed.commands if c.words and c.words[0].text.lower() == 'write-output']
        for text in ('powershell %s-c "Write-Output hi"' % ('-i ' * 16),
                     'echo "Write-Output hi" | powershell -i -i -i -i -Zork'):
            with self.subTest(text):
                self.assertEqual(len(said(text)), 1)

    def test_an_unclosed_block_comment_is_not_read(self):
        # shell._Scan.tokens: powershell.exe runs no -Command text whose <# has no #> ("The
        # terminator '#>' is missing from the multiline comment", probe10.log); the line is flagged
        for text in ('Write-Output hi <# rest', 'powershell -c "Write-Output hi <# rest"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'unreadable')
        for text in ('Write-Output hi <# rest #>', 'powershell -c "Write-Output hi <# rest #>"'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_a_double_dash_switch_is_read_as_pwsh_reads_it(self):
        # pwsh reads --c and --command as -Command; powershell.exe runs the unknown --c and what
        # follows as the command: both readings are judged
        for text in ('pwsh --c "Remove-Item {main}\\x.txt"', 'pwsh --command "Remove-Item {main}\\x.txt"',
                     'powershell --c "Remove-Item {main}\\x.txt"'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'main-checkout')
        self.allowed(self.run_ps('pwsh --command "Write-Output hi"'))

    def test_a_value_switch_powershell_exe_does_not_know_is_no_value(self):
        # commands.PS51_SWITCHES: value switches powershell.exe 5.1 does not know, which it runs,
        # and every word after, as the command (observed: probe8.log)
        for sw in ('-ConfigurationFile', '-SettingsFile', '-CustomPipeName', '-conf', '-PSConsoleFile'):
            with self.subTest(sw):
                self.denied(self.run_ps('powershell %s "; Remove-Item {main}\\x.txt"' % sw), 'main-checkout')

    def test_a_pwsh_only_switch_is_unknown_to_powershell_exe(self):
        # commands.PS51_SWITCHES: -Login, -l, -Interactive and -NoProfileLoadTime are pwsh's; to
        # powershell.exe the switch and what follows are the command; pwsh reads standard input
        for sw in ('-Login', '-l', '-Interactive', '-NoProfileLoadTime', '-mt', '-cwa'):
            with self.subTest(sw):
                self.denied(self.run_ps('powershell %s -ep "; Remove-Item {main}\\x.txt"' % sw), 'main-checkout')
        self.denied(self.run_ps("'Remove-Item {main}\\x.txt' | pwsh -NoProfileLoadTime"), 'main-checkout')

    def test_a_version_prefix_powershell_exe_rejects_is_no_value(self):
        # powershell.exe 5.1 knows no -Version prefix as a switch and runs it as the command
        for sw in ('-v', '-ve', '-Version'):
            with self.subTest(sw):
                self.denied(self.run_ps('powershell %s "; Remove-Item {main}\\x.txt"' % sw), 'main-checkout')

    def test_a_literal_here_string_value_passes(self):
        # rules._ps_value_end: a -Value here-string with no substitution is one value
        for q in ("'", '"'):
            with self.subTest(q):
                self.allowed(self.run_ps('Set-Content -Path x.txt -Value @%s\nline one\r\nline two\n%s@' % (q, q)))

    def test_a_computed_value_or_a_positional_group_stays_refused(self):
        # rules.ps_unread: $( ) runs code, a positional ( ) could be the path, and a here-string line
        # PowerShell closes on (a typographic quote) the scanner does not read as its end
        for text in (r'Set-Content x.txt -Value "Generated $(Get-Date)"', r'Set-Content x.txt -Value $(Get-Date)',
                     r"Set-Content x.txt ($a -join ',')", 'Set-Content -Path x.txt -Value @"\n$(Get-Date)\n"@',
                     "Set-Content -Path x.txt -Value @'\n’@; Set-Content {main}\\x.txt hi\n'@",
                     "Set-Content x.txt @'\nline one\n'@"):
            with self.subTest(text):
                self.refused_for_both(text)

    def test_cd_inside_a_subexpression_makes_the_location_unknown(self):
        # $( ) and ( ) run in the caller's scope: the cd moves the location for what follows
        for text in (r'echo $(cd {main}) ; New-Item x.txt', r'echo (cd {main}) ; New-Item x.txt'):
            with self.subTest(text):
                self.denied(self.run_ps(text), 'unplaced')


class Unchanged(Base):
    """What the fix leaves as it was."""

    def test_dynamic_targets_outside_powershell_and_reads(self):
        self.allowed(self.run_bash('p={main}/x.txt; echo 1 > "$p"'))
        self.allowed(self.run_ps(r'git status 2>$null'))
        self.allowed(self.run_ps(r'$p="{main}\x.txt"; Get-Content $p'))

    def test_owner_session_dynamic_target_is_not_refused(self):
        self.allowed(self.run_ps(r'$p="{main}\x.txt"; Set-Content $p x', sub=False))

    def test_parenthesised_values_that_are_not_targets(self):
        for text in (r'New-Item x.txt -Value (Get-Date)', r'Write-Output (Get-Date)',
                     r'$x = (Get-Date); New-Item y.txt', r'if (Test-Path x) {{ New-Item y.txt }}'):
            with self.subTest(text):
                self.allowed(self.run_ps(text))

    def test_missing_one_letter_drive_still_refused(self):
        letter = missing_drive()
        self.denied(self.run_ps('New-Item %s:\\x.txt' % letter), 'write-jail')

    def test_filesystem_provider_into_the_worktree_is_allowed(self):
        self.allowed(self.run_ps(r'New-Item FileSystem::{wt}\x.txt'))
        self.allowed(self.run_ps(r'echo 1 > Microsoft.PowerShell.Core\FileSystem::{wt}\x.txt'))

    def test_relative_and_absolute_writes(self):
        self.allowed(self.run_ps(r'New-Item x.txt'))
        self.allowed(self.run_ps(r'New-Item src\x.txt'))
        self.allowed(self.run_ps(r'New-Item {wt}\x.txt'))
        self.denied(self.run_ps(r'New-Item {main}\x.txt'), 'main-checkout')

    def test_owner_session_is_not_refused(self):
        self.allowed(self.run_ps(r'New-Item Main:\x.txt', sub=False))
        self.allowed(self.run_ps(r'cd Main:\; New-Item x.txt', sub=False))

    def test_jailed_session_cd_back_to_a_known_place_writes_as_before(self):
        self.allowed(self.run_ps(r'cd Main:\; cd {wt}; New-Item x.txt', env=COORDINATOR, sub=False))

    def test_bash_colon_words_are_files_in_the_worktree(self):
        self.allowed(self.run_bash('echo 1 > Main:x.txt'))
        self.allowed(self.run_bash('echo 1 > "FileSystem::{main}/x.txt"'))
        self.allowed(self.run_bash('touch C::x'))


class ResolvePath(unittest.TestCase):
    """shell.resolve_path and shell.ps_unplaceable on their own."""

    CWD = 'C:\\w\\wt'

    def test_bash_and_cmd_unchanged(self):
        for shell in (None, sh.BASH, sh.CMD):
            with self.subTest(shell):
                self.assertEqual(sh.resolve_path('FileSystem::C:\\m\\x', self.CWD, shell),
                                 'C:\\w\\wt\\FileSystem::C:\\m\\x')
                self.assertEqual(sh.resolve_path('Main:\\x', self.CWD, shell), 'C:\\w\\wt\\Main:\\x')
                self.assertEqual(sh.resolve_path('C::x', self.CWD, shell), None)
                self.assertEqual(sh.resolve_path('C:\\m\\x', self.CWD, shell), 'C:\\m\\x')

    def test_powershell(self):
        cases = {
            'FileSystem::C:\\m\\x': 'C:\\m\\x',
            'filesystem::C:/m/x': 'C:\\m\\x',
            'Microsoft.PowerShell.Core\\FileSystem::C:\\m\\x': 'C:\\m\\x',
            'FileSystem::\\\\srv\\share\\x': '\\\\srv\\share\\x',
            'FileSystem::x.txt': None,              # relative: whose current folder is not shown
            'Other.Module\\FileSystem::C:\\m\\x': None,
            'Registry::HKCU\\x': None,
            'Main:\\x': None,
            'Temp:\\x': None,
            'env:FOO': None,
            'C:x': None,                            # one-letter drive-relative: as before
            'C:\\m\\x': 'C:\\m\\x',
            'x.txt': 'C:\\w\\wt\\x.txt',
            '.\\a:b': 'C:\\w\\wt\\a:b',
            '\\\\srv\\share\\x': '\\\\srv\\share\\x',
        }
        for text, want in cases.items():
            with self.subTest(text):
                self.assertEqual(sh.resolve_path(text, self.CWD, sh.PS), want)

    def test_unplaceable(self):
        for text in ('Main:\\x', 'Main:x', 'Registry::x', 'Foo\\Bar::x', 'FileSystem::rel', 'env:FOO',
                     '1:\\x', '_:x', 'é:\\x', '12:\\x'):
            with self.subTest(text):
                self.assertTrue(sh.ps_unplaceable(text))
        for text in ('', 'C:\\x', 'C:x', 'x.txt', '.\\a:b', '\\\\srv\\s', '//srv/s',
                     'FileSystem::C:\\x', 'Microsoft.PowerShell.Core\\FileSystem::\\\\srv\\s'):
            with self.subTest(text):
                self.assertFalse(sh.ps_unplaceable(text))


setUpModule = W.deny_softened           # these pin what the guard cannot place and the dropped rules (world.deny_softened)
tearDownModule = W.allow_unplaced


if __name__ == '__main__':
    unittest.main()
