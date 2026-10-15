"""kitlib.hubsite.todosteps: owner to-do steps rendered as PowerShell, and the refusal rules for
an agent's to-do. The PowerShell itself is run once, batched, in Windows PowerShell 5.1 against
temporary files (skipped where powershell.exe is absent)."""
import base64
import os
import re
import shutil
import tempfile
import unittest

from kitlib.hubsite import todosteps
from kitlib.hubsite.todosteps import Refused, StepError
from kitlib.proc import spawn

POWERSHELL = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32',
                          'WindowsPowerShell', 'v1.0', 'powershell.exe')
P = r'C:\work\notes.txt'
B64_RE = re.compile(r"FromBase64String\('([A-Za-z0-9+/=]*)'\)")
# bidi controls, zero-width characters, the BOM and the no-break space: each makes a literal read
# other than it runs, or look like a plain space
BIDI_AND_INVISIBLE = ([chr(c) for c in range(0x202a, 0x202f)] + [chr(c) for c in range(0x2066, 0x206a)]
                      + [chr(c) for c in range(0x200b, 0x2010)] + ['﻿', ' '])


def edit(path=P, old='a', new='b'):
    return {'edit': path, 'old': old, 'new': new}


def one(step):
    [cmd] = todosteps.render([step])
    return cmd


def payloads(cmd):
    return [base64.b64decode(m).decode('utf-8') for m in B64_RE.findall(cmd)]


class Shapes(unittest.TestCase):
    def refused(self, steps, field='steps'):
        with self.assertRaises(StepError) as c:
            todosteps.render(steps)
        self.assertEqual(c.exception.field, field)
        return str(c.exception)

    def test_steps_must_be_a_non_empty_list_of_objects(self):
        self.refused('x')
        self.refused([])
        self.refused(['x'])

    def test_a_step_names_exactly_one_verb_and_no_other_key(self):
        self.refused([{}])
        self.refused([{'edit': P, 'open': P, 'old': 'a', 'new': 'b'}])
        self.refused([dict(edit(), extra=1)])
        self.refused([{'open': P, 'old': 'a'}])
        self.refused([{'run': 'x', 'line': 2}])

    def test_the_message_names_the_step(self):
        self.assertIn('steps[1]', self.refused([{'run': 'x'}, {'open': 'rel.txt'}]))

    def test_a_path_is_absolute_text_with_no_control_or_quote_character(self):
        for bad in ('rel.txt', r'\rooted.txt', 'C:', 'C:\\dir\\', 'C:\\a\x07b.txt', 'C:\\a\nb.txt',
                    'C:\\a"b.txt', '', 3, None):
            with self.subTest(path=bad):
                self.refused([edit(path=bad)])
                self.refused([{'open': bad}])

    def test_absolute_and_unc_paths_pass(self):
        for good in (P, 'D:/x/y.md', r'\\server\share\f.txt'):
            with self.subTest(path=good):
                todosteps.render([edit(path=good), {'open': good}])

    def test_old_is_non_empty_text_and_new_differs(self):
        self.refused([edit(old='')])
        self.refused([edit(old='a', new='a')])
        self.refused([edit(old='a\r\nb', new='a\nb')])     # the same once line endings are LF
        self.refused([edit(old=1)])
        self.refused([edit(new=None)])

    def test_old_and_new_have_a_size_cap(self):
        self.refused([edit(old='x' * (todosteps.TEXT_MAX + 1))])
        self.refused([edit(new='x' * (todosteps.TEXT_MAX + 1))])
        todosteps.render([edit(old='x' * todosteps.TEXT_MAX)])

    def test_an_empty_new_deletes(self):
        self.assertIn("$n=''", one(edit(new='')))

    def test_open_takes_a_positive_line(self):
        self.refused([{'open': P, 'line': 0}])
        self.refused([{'open': P, 'line': True}])
        self.refused([{'open': P, 'line': '3'}])
        self.assertIn("'-n12'", one({'open': P, 'line': 12}))

    def test_open_finds_notepad_plus_plus_under_either_program_files_folder(self):
        cmd = one({'open': r'\\server\share\f.txt'})
        self.assertIn('$env:ProgramFiles', cmd)
        self.assertIn('${env:ProgramFiles(x86)}', cmd)
        self.assertIn(todosteps.NOTEPADPP, cmd)
        self.assertNotRegex(cmd, r'[A-Za-z]:\\')      # no drive is written into the command
        self.assertNotIn('\n', cmd)

    def test_run_is_non_empty_text_passed_verbatim(self):
        self.refused([{'run': ' '}])
        self.refused([{'run': ['x']}])
        self.assertEqual(one({'run': 'python tools/x.py --y'}), 'python tools/x.py --y')

    def test_one_command_per_step_in_order(self):
        cmds = todosteps.render([edit(), {'open': P}, {'run': 'x'}])
        self.assertEqual(len(cmds), 3)
        self.assertIn('notepad++.exe', cmds[1])
        self.assertEqual(cmds[2], 'x')


class Literals(unittest.TestCase):
    def test_plain_text_is_a_readable_one_line_literal(self):
        cmd = one(edit(path=r'C:\a b\$x `y\f.txt', old='$(Write-Output PWNED)', new='`$HOME'))
        self.assertNotIn('\n', cmd)
        self.assertNotIn('FromBase64String', cmd)
        self.assertIn(r"$p='C:\a b\$x `y\f.txt'", cmd)
        self.assertIn("$o='$(Write-Output PWNED)'", cmd)
        self.assertIn("$n='`$HOME'", cmd)
        self.assertTrue(cmd.startswith('& {') and cmd.endswith('}'))

    def test_multi_line_text_stays_readable_and_spans_lines_only_inside_it(self):
        cmd = one(edit(old='one\r\ntwo', new='three\nfour\nfive'))
        self.assertNotIn('FromBase64String', cmd)
        self.assertNotIn('\r', cmd)
        self.assertIn("$o='one\ntwo'", cmd)
        self.assertEqual(cmd.count('\n'), 3)

    def test_any_single_quote_form_falls_back_to_base64(self):
        for q in ("'", '\u2018', '\u2019', '\u201a', '\u201b'):
            with self.subTest(quote=hex(ord(q))):
                cmd = one(edit(old='it%ss' % q, new='x'))
                self.assertEqual(payloads(cmd), ['it%ss' % q])
                self.assertIn("it%ss" % q, cmd[:cmd.index('#>')])     # the preview shows it
                self.assertNotIn('it%ss' % q, cmd[cmd.index('#>'):])

    def test_control_characters_tabs_and_astral_text_fall_back_to_base64(self):
        for text in ('a\x1bb', 'a\tb', 'a\x00b', 'smile \U0001F600', 'a\u2028b', 'a\x85b'):
            with self.subTest(text=repr(text)):
                self.assertEqual(payloads(one(edit(old=text))), [text])

    def test_bidi_and_invisible_characters_fall_back_to_base64_and_show_escaped(self):
        for ch in BIDI_AND_INVISIBLE:
            with self.subTest(ch=hex(ord(ch))):
                text = 'a%sb' % ch
                cmd = one(edit(old=text, new='y'))
                self.assertEqual(payloads(cmd), [text])
                [pv] = re.findall(r'<#(.*?)#>', cmd)
                self.assertNotIn(ch, pv)
                self.assertIn('a%sb' % (('\\x%02x' if ord(ch) < 0x100 else '\\u%04x') % ord(ch)),
                              pv)
                self.assertNotIn(ch, cmd)

    def test_a_lone_surrogate_is_a_bad_step_not_an_internal_error(self):
        for s in ('\ud800', '\udfff', '\udc00'):
            for step in (edit(old='a%sb' % s), edit(new='x%s' % s), edit(old="'%s" % s),
                         edit(path='C:\\a%s.txt' % s), {'open': 'C:\\a%s.txt' % s}):
                with self.subTest(step=ascii(step)):
                    with self.assertRaises(StepError) as c:
                        todosteps.render([step])
                    self.assertEqual(c.exception.field, 'steps')
                    self.assertIn('surrogate', str(c.exception))

    def test_a_base64_text_has_a_readable_single_line_preview(self):
        text = "it's\na\tlong line " + 'x' * 100 + ' #> <# end'
        cmd = one(edit(old=text, new='y'))
        previews = re.findall(r'<#(.*?)#>', cmd)
        self.assertEqual(len(previews), 1)
        pv = previews[0]
        self.assertIn("it's\\na\\tlong", pv)
        self.assertLessEqual(len(pv), 80)
        self.assertNotIn('\n', cmd)                 # the whole block is still one line

    def test_a_hostile_path_falls_back_to_base64(self):
        path = "C:\\it's \u2019q\u2019\\f.txt"
        cmd = one(edit(path=path))
        self.assertEqual(payloads(cmd), [path])
        self.assertEqual(payloads(one({'open': path})), [path])

    def test_base64_is_utf8_of_the_lf_text(self):
        cmd = one(edit(old="x'\r\ny", new='\u00e9'))
        self.assertEqual(payloads(cmd), ["x'\ny"])
        self.assertIn("$n='\u00e9'", cmd)


class Summary(unittest.TestCase):
    def test_each_step_says_what_it_does(self):
        self.assertEqual(
            todosteps.summary([edit(old='a\nb\nc'), edit(old='a'), {'open': P, 'line': 3},
                               {'run': 'python x.py ' + 'y' * 100}]),
            ['replace 3 lines in %s' % P, 'replace 1 line in %s' % P,
             'open %s at line 3 in Notepad++' % P,
             'run python x.py ' + 'y' * 48 + '...'])

    def test_summary_checks_the_steps_too(self):
        with self.assertRaises(StepError):
            todosteps.summary([{'open': 'rel'}])


class Check(unittest.TestCase):
    def refused(self, field, **fields):
        with self.assertRaises(Refused) as c:
            todosteps.check(fields)
        self.assertEqual(c.exception.field, field)
        return str(c.exception)

    def passes(self, **fields):
        todosteps.check(fields)

    def test_plain_notepad_is_refused_and_notepad_plus_plus_is_not(self):
        for cmd in ('notepad C:\\x.txt', 'notepad.exe x',
                    'C:\\Windows\\notepad.exe x', '& notepad', 'notepad'):
            with self.subTest(cmd=cmd):
                self.assertIn('Notepad++', self.refused('commands', commands=[cmd]))
                self.refused('steps', steps=[{'run': cmd}])
        for cmd in ('notepad++ x', '& "C:\\Program Files\\Notepad++\\notepad++.exe" x',
                    'notepad2 x', 'type C:\\notepads\\x.txt', 'type notepad.txt',
                    'cd C:\\notepad\\x', 'dir D:\\tools\\notepad\\'):
            with self.subTest(cmd=cmd):
                self.passes(commands=[cmd])

    def test_open_and_apply_prose_is_refused(self):
        for field, text in (('detail', 'Open the file and apply the change below.'),
                            ('title', 'open config.json then edit the port'),
                            ('detail', 'Please apply these edits to settings.json'),
                            ('detail', 'make the following changes by hand'),
                            ('commands', '# open it and replace the line')):
            with self.subTest(text=text):
                fields = {field: [text] if field == 'commands' else text}
                self.assertIn('edit', self.refused(field, **fields))
        self.passes(title='Restart the hub', detail='Open the Action Hub page. Then apply the '
                                                    'filter? No: just look.')
        self.passes(detail='Reopen the hub, then apply nothing.')

    def test_coordinator_work_is_refused(self):
        for cmd in ('git worktree add ../x -b y', 'git -C Z:/r worktree remove --force x',
                    'git worktree prune', 'git worktree move a b',
                    'python tools/gate.py --full', 'python Z:/k/tools/land.py --submit x',
                    'python tools/new_worktree.py kit/x', 'python tools/worktree_remove.py x',
                    'python tools/sync_base.py', 'git branch -D kit/x', 'git branch -rd o/x',
                    'git branch --delete x', 'git tag -d v1', 'git update-ref -d refs/heads/x',
                    'git push origin --delete x', 'git push origin :x', 'git push -d origin x',
                    'git push --force origin x', 'git push origin +x',
                    'git push -fu origin x', 'git push -uf origin x', 'git push -f origin x',
                    'git push --force-if-includes origin x',
                    'git push --force-with-lease --force-if-includes origin x',
                    'cd Z:/r; git worktree add a'):
            with self.subTest(cmd=cmd):
                self.assertIn('coordinator', self.refused('commands', commands=[cmd]))
                self.refused('steps', steps=[{'run': cmd}])
        for cmd in ('git worktree list', 'git push origin x', 'git branch -v', 'git tag v1',
                    'git push -u origin x', 'git push --follow-tags origin x',
                    'git status', 'python tools/backlog.py next', 'git log --grep gate'):
            with self.subTest(cmd=cmd):
                self.passes(commands=[cmd])

    def test_generated_edit_code_and_edit_text_are_not_judged(self):
        # an edit may well write the word notepad, or prose, into a file
        self.passes(steps=[edit(old='use notepad', new='open it and apply the change'),
                           {'open': r'C:\notepad\x.txt'}])

    def test_the_message_names_the_command(self):
        self.assertIn('commands[1]', self.refused('commands', commands=['ok', 'notepad x']))
        self.assertIn('steps[0]', self.refused('steps', steps=[{'run': 'notepad x'}]))

    def test_fields_of_other_types_are_left_to_the_schema(self):
        self.passes(title=None, detail=3, commands='notepad x', steps='x')
        self.passes(commands=[3, None], steps=[3, {'run': 4}])

    def test_a_clean_to_do_passes(self):
        self.passes(title='Rotate the token', detail='The hub token expired.',
                    commands=['python tools/hub.py setup'], steps=[edit(), {'open': P}])


# ---------------------------------------------------------------------------- real PowerShell
BOM8 = b'\xef\xbb\xbf'


def u16(text):
    return b'\xff\xfe' + text.encode('utf-16-le')


# name -> (folder name, file bytes or None (no file), old, new, expected bytes, expected line start)
CASES = [
    ('utf8-lf', 'plain', b'alpha\nbeta\ngamma\n', 'beta', 'BETA',
     b'alpha\nBETA\ngamma\n', 'ok '),
    ('bom-crlf-multiline', 'plain', BOM8 + b'a\r\nb\r\nc\r\n', 'a\nb', 'x\ny\nz',
     BOM8 + b'x\r\ny\r\nz\r\nc\r\n', 'ok '),
    ('utf16', 'plain', u16('hello w\u00f6rld\r\n'), 'w\u00f6rld', 'world \u2713',
     u16('hello world \u2713\r\n'), 'ok '),
    ('twice', 'plain', b'dup dup', 'dup', 'x', b'dup dup', 'MISS '),
    ('overlap', 'plain', b'aaa', 'aa', 'b', b'aaa', 'MISS '),
    ('absent', 'plain', b'abc', 'zzz', 'y', b'abc', 'MISS '),
    ('ansi', 'plain', b'caf\xe9 x', 'x', 'y', b'caf\xe9 x', 'MISS '),
    ('mixed-eol', 'plain', b'a\r\nb\nc', 'a\nb', 'q', b'a\r\nb\nc', 'MISS '),
    ('mixed-eol-one-line', 'plain', b'a\r\nb\nc', 'c', 'q', b'a\r\nb\nq', 'ok '),
    ('delete', 'plain', b'keep drop keep', ' drop', '', b'keep keep', 'ok '),
    ('tab-and-quote', 'plain', b"x\ty 'z'", "\ty 'z'", "\t'@ ok", b"x\t'@ ok", 'ok '),
    ('missing', 'plain', None, 'a', 'b', None, 'MISS '),
    ('hostile-path', "it's $(Write-Output P1) `n \u2018q\u2019 \u00e9", b'v $x\n',
     '$x', "x'@\n$(Write-Output P2) `\"q\" \u2019", None, 'ok '),
    ('readable-path', 'dir $(Write-Output P3) `t', b'see $(Write-Output P4)\n',
     '$(Write-Output P4)', '`$HOME ${env:PATH} @(1)', b'see `$HOME ${env:PATH} @(1)\n', 'ok '),
]


WHERE = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'where.exe')
# name -> (ProgramFiles, ProgramFiles(x86)): 'empty' has no Notepad++, 'x86' has the stand-in,
# None is unset. A name ending -found expects ok, every other MISS.
OPEN_CASES = [
    ('open-in-program-files-found', 'x86', 'empty'),
    ('open-in-x86-found', 'empty', 'x86'),
    ('open-nowhere', 'empty', 'empty'),
    ('open-no-x86-variable', 'empty', None),
]


@unittest.skipUnless(os.path.isfile(POWERSHELL) and os.path.isfile(WHERE),
                     'no Windows PowerShell 5.1 here')
class RealPowerShell(unittest.TestCase):
    """Every case through one powershell.exe run: the bytes each leaves, and its ok/MISS line."""

    @classmethod
    def setUpClass(cls):
        root = tempfile.mkdtemp(prefix='kit-todosteps-')
        cls.root = root
        cls.paths = {}
        cls.commands = {}
        lines = []
        for name, folder, data, old, new, _, _ in CASES:
            d = os.path.join(root, name, folder)
            os.makedirs(d)
            path = os.path.join(d, 'f.txt')
            if data is not None:
                with open(path, 'wb') as f:
                    f.write(data)
            cls.paths[name] = path
            cmd = one(edit(path=path, old=old, new=new))
            cls.commands[name] = cmd
            lines += ["'--- %s'" % name, cmd]
        # the open steps last, as they set the Program Files variables for the rest of the run.
        # A stand-in Notepad++ is a copy of where.exe, which only reads: its complaint about the
        # arguments is dropped (2>$null); the case's ok or MISS line is what is read.
        empty = os.path.join(root, 'pf-empty')
        x86 = os.path.join(root, 'pf-x86')
        os.makedirs(empty)
        os.makedirs(os.path.join(x86, 'Notepad++'))
        shutil.copyfile(WHERE, os.path.join(x86, 'Notepad++', 'notepad++.exe'))
        cls.open_path = os.path.join(root, 'open', 'f.txt')
        opened = one({'open': cls.open_path, 'line': 2}) + ' 2>$null'
        for name, pf, pf86 in OPEN_CASES:
            setenv = ["$env:ProgramFiles='%s'" % (empty if pf == 'empty' else x86)]
            setenv.append("Remove-Item 'Env:ProgramFiles(x86)' -ErrorAction SilentlyContinue"
                          if pf86 is None else
                          "${env:ProgramFiles(x86)}='%s'" % (empty if pf86 == 'empty' else x86))
            lines += setenv + ["'--- %s'" % name, opened]
        script = os.path.join(root, 'run.ps1')
        with open(script, 'wb') as f:            # 5.1 reads a script without a BOM as ANSI
            f.write(BOM8 + '\r\n'.join(lines).encode('utf-8'))
        r = spawn.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                       '-ExecutionPolicy', 'Bypass', '-File', script],
                      capture_output=True, timeout=120)
        cls.stderr = r.stderr.decode('utf-8', 'replace')
        out = r.stdout.decode('utf-8', 'replace')
        cls.out = out
        cls.lines = {}
        cur = None
        for line in out.splitlines():
            if line.startswith('--- '):
                cur = line[4:]
                cls.lines[cur] = []
            elif cur is not None and line.strip():
                cls.lines[cur].append(line)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, True)

    def test_each_case_prints_one_ok_or_miss_line_and_leaves_these_bytes(self):
        self.assertEqual(self.stderr.strip(), '')
        for name, folder, data, old, new, want, start in CASES:
            with self.subTest(case=name):
                got = self.lines.get(name)
                self.assertEqual(len(got or []), 1, (got, self.out))
                self.assertTrue(got[0].startswith(start), got[0])
                path = self.paths[name]
                if want is None and data is None:
                    self.assertFalse(os.path.exists(path))
                    continue
                with open(path, 'rb') as f:
                    have = f.read()
                if want is None:            # the hostile case: the expected text, built here
                    want = ('v ' + new + '\n').encode('utf-8')
                self.assertEqual(have, want)

    def test_a_miss_says_how_many_times_or_why(self):
        self.assertIn('2 times', self.lines['twice'][0])
        self.assertIn('2 times', self.lines['overlap'][0])
        self.assertIn('0 times', self.lines['absent'][0])
        self.assertIn('mixed', self.lines['mixed-eol'][0])

    def test_open_runs_notepad_plus_plus_from_either_program_files_folder_or_misses(self):
        for name, _pf, _pf86 in OPEN_CASES:
            with self.subTest(case=name):
                got = [l for l in self.lines.get(name, []) if l.startswith(('ok ', 'MISS '))]
                self.assertEqual(len(got), 1, (self.lines.get(name), self.out))
                want = 'ok ' if name.endswith('-found') else 'MISS '
                self.assertEqual(got[0][:len(want)], want, got[0])
                self.assertIn(self.open_path, got[0])
                if want == 'MISS ':
                    self.assertIn('Notepad++', got[0])

    def test_no_injected_command_ran(self):
        for mark in ('P1', 'P2', 'P3', 'P4'):
            self.assertNotIn(mark + '\n', self.out.replace('\r', ''))
        self.assertNotIn('FromBase64String', self.commands['readable-path'])
        self.assertIn('FromBase64String', self.commands['hostile-path'])

    def test_the_open_commands_parse(self):
        cmds = [one({'open': p, 'line': 4}) for p in (P, "C:\\it's\\f.txt")]
        script = os.path.join(self.root, 'parse.ps1')
        body = ["$e=$null; $null=[System.Management.Automation.Language.Parser]::ParseInput("
                "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('%s')),[ref]$null,"
                "[ref]$e); 'errors ' + $e.Count"
                % base64.b64encode(c.encode('utf-8')).decode('ascii') for c in cmds]
        with open(script, 'wb') as f:
            f.write(BOM8 + '\r\n'.join(body).encode('utf-8'))
        r = spawn.run([POWERSHELL, '-NoLogo', '-NoProfile', '-NonInteractive',
                       '-ExecutionPolicy', 'Bypass', '-File', script],
                      capture_output=True, timeout=60)
        self.assertEqual(r.stdout.decode().split(), ['errors', '0', 'errors', '0'])


if __name__ == '__main__':
    unittest.main()
