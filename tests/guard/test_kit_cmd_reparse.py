"""cmd-reparse: a batch file (.cmd, .bat, and the bare kit command, which machine setup puts on
the PATH as kit.cmd) runs in cmd.exe, which reads its command line again before the program
does, so an argument holding one of cmd's characters is refused (docs/guard.md "Batch files and
cmd.exe"). The same arguments through python and tools/kit.py are not this rule's."""
import unittest

from kitlib.guard import shell as sh

from . import world as W
from .claudehost import decide
from .world import bash, ps

RULE = 'cmd-reparse'


class CmdReparse(unittest.TestCase):

    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)
        self.kit = W.fwd(self.w.kit_dir)

    def judge(self, shell, text):
        make = bash if shell == 'bash' else ps
        return decide(make(text, self.w.wt, True), self.w.ctx())

    def kit_rows(self):
        """(shell, text) runs of the kit command whose arguments cmd.exe would read again."""
        return [
            ('ps', "kit status 'x&y'"),
            ('ps', "kit status 'x|y'"),
            ('ps', "kit status 'a>b'"),
            ('ps', "kit status 'a<b'"),
            ('ps', "kit status '-^C' Z:/x"),
            ('ps', "kit status '50%OS%'"),
            ('ps', "kit status 'q\"r' 'a&b c'"),
            ('ps', "kit status 'a!OS!b'"),
            ('ps', "kit status 'a(b' 'c)'"),
            ('ps', "kit status 'a\nb' tail"),
            ('ps', 'kit status $a'),
            ('ps', 'kit status @a'),
            ('ps', 'kit --% status a&b'),
            ('ps', "kit.cmd status 'x&y'"),
            ('ps', "KIT.BAT status 'x&y'"),
            ('ps', "kit.cmd. status 'x&y'"),
            ('ps', "Z:/x/bin/kit.cmd status 'x&y'"),
            ('ps', "& \"%s/bin/kit.cmd\" status 'x&y'" % self.kit),
            ('ps', "& kit status 'x&y'"),
            ('ps', "Start-Process kit -ArgumentList 'status x&y' -Wait"),
            ('ps', 'cmd /c "kit.cmd status x^&y"'),
            ('ps', 'cmd /c "call kit.cmd status x^&y"'),
            ('ps', 'cmd /c "@kit status x^|y"'),
            ('bash', "kit status 'x&y'"),
            ('bash', "kit.cmd status '-^C' Z:/x"),
            ('bash', "./kit.cmd status '50%OS%'"),
            ('bash', "kit.cmd status 'q\"r' 'a&echo b c'"),
            ('bash', 'kit.cmd status "$a"'),
            ('bash', 'git status; kit status "x&y"'),
            ('bash', 'printf x | xargs kit.cmd status'),
            # an unquoted bash wildcard: bash fills in file names (one may be named x&whoami)
            ('bash', 'kit status x*'),
            ('bash', 'kit.cmd status Z:/x/[ab]'),
            ('bash', "kit status 'a b'/?"),
            # Git Bash's start: a script that hands every word to cmd.exe's start, & escaped only
            ('bash', "start kit.cmd 'x&whoami'"),
            ('bash', "start kit status 'x|y'"),
            ('bash', "start //b //wait kit status '50%OS%'"),
            ('bash', "start '' kit status 'a^b'"),
            ('bash', "start //d Z:/x kit status 'a>b'"),
            ('bash', "/usr/bin/start kit status 'a<b'"),
            ('bash', 'start kit status "$x"'),
            ('bash', 'start kit status x*'),
            # cmd ends a command name at , ; = ( and /, inside its own text
            ('ps', 'cmd /c "kit,status x^&y"'),
            ('ps', 'cmd /c "call;kit status x^&y"'),
            ('ps', 'cmd /c "kit.cmd,status x^&y"'),
            ('ps', 'cmd /c "kit=status x^|y"'),
            ('ps', 'cmd /c "kit/status x^&y"'),
            ('ps', 'cmd /k "kit,status x^&y"'),
            ('ps', 'cmd /r "call,kit;status x^|y"'),
            ('bash', "cmd //c 'kit;status x^&y'"),
        ]

    def refused(self, shell, text):
        d = self.judge(shell, text)
        self.assertIsNotNone(d, 'allowed')
        self.assertEqual((d.verdict, d.rule), ('deny', RULE), d.reason)
        return d

    def test_a_kit_run_with_cmds_characters_is_refused(self):
        for shell, text in self.kit_rows():
            with self.subTest(text):
                d = self.refused(shell, text)
                self.assertIn('/tools/kit.py', d.instead)
                self.assertIn('cmd.exe', d.reason)

    def test_any_batch_file_with_cmds_characters_is_refused(self):
        for shell, text in (('ps', "./foo.bat 'a&b'"),
                            ('ps', "& 'Z:/x/tools/godot.cmd' --path 'a(b)'"),
                            ('ps', "tools\\godot.cmd --path '50%x%'"),
                            ('bash', "./run.CMD 'a|b'"),
                            ('bash', 'tools/godot.cmd --path "$p"')):
            with self.subTest(text):
                d = self.refused(shell, text)
                self.assertNotIn('kit.py', d.instead)

    def test_an_unquoted_bash_wildcard_in_a_batch_files_arguments_is_refused(self):
        for text in ('tools/godot.cmd --path *.tscn', './run.bat x?', 'kit.cmd status {a,b}*'):
            with self.subTest(text):
                d = self.refused('bash', text)
                self.assertIn('wildcard', d.reason)

    def test_any_program_git_bash_start_runs_with_cmds_characters_is_refused(self):
        """Git Bash's start hands its words to cmd.exe whatever it starts (OBSERVED: an exe got
        50%OS% expanded, a^b as ab, a>f redirected, a|sort piped), so the program need not be a
        batch file."""
        for text in ("start //b python x.py 'a%OS%'",
                     "start x.py 'a>b'",
                     "start //b //wait notepad 'a|b'",
                     "start 'https://e.x/?a=1&b=2'",
                     'start "$p" x',
                     'start notepad x*',
                     "start //b tools/godot.cmd --path 'a(b)'",
                     'printf x | xargs start notepad'):
            with self.subTest(text):
                d = self.refused('bash', text)
                self.assertNotIn('kit.py', d.instead)
                self.assertIn('start', d.reason)

    def test_plain_arguments_pass(self):
        for shell, text in (('ps', 'kit status -C Z:/x'),
                            ('ps', 'kit.cmd status -C Z:/x'),
                            ('ps', "kit status -C 'Z:/a b/x' --why 'a, b; c=d'"),
                            ('ps', "& \"%s/bin/kit.cmd\" status -C Z:/x" % self.kit),
                            ('ps', "Start-Process kit -ArgumentList 'status -C Z:/x' -Wait"),
                            ('ps', './foo.bat plain words'),
                            ('bash', 'kit status -C Z:/x'),
                            ('bash', './kit.cmd status -C Z:/x'),
                            ('bash', "kit status 'x*' \"y?\" z\\[ $'w*'"),
                            ('bash', 'start kit status -C Z:/x'),
                            ('bash', 'start //b //wait kit status -C Z:/x'),
                            ('ps', 'cmd /c "kit status -C Z:/x"'),
                            ('ps', 'cmd /c "kit,status -C Z:/x"')):
            with self.subTest(text):
                self.assertIsNone(self.judge(shell, text))

    def test_the_same_arguments_through_python_are_not_this_rules(self):
        for shell, text in (('ps', "python \"%s/tools/kit.py\" status 'x&y' '50%%OS%%' 'q\"r'" % self.kit),
                            ('bash', "python \"%s/tools/kit.py\" status 'x&y' '-^C'" % self.kit),
                            ('ps', "kit.exe status 'x&y'"),
                            ('ps', "./foo.ps1 'a&b'"),
                            ('bash', "echo 'kit.cmd x&y'"),
                            ('ps', 'kit status x*'),                    # PowerShell does not glob
                            ('bash', 'python x.py x*'),                 # no cmd.exe reads it
                            ('bash', 'start //b python x.py plain'),
                            ('ps', 'cmd /c "kitty,status a^&b"'),
                            ('ps', 'cmd /c "kit@x status a^&b"'),       # @ ends no command name
                            ('ps', 'cmd /c "kit+x status a^&b"')):      # nor does +
            with self.subTest(text):
                d = self.judge(shell, text)
                self.assertTrue(d is None or d.rule != RULE, d and d.reason)

    def test_each_instead_runs_clear_of_the_rule(self):
        for shell, text in self.kit_rows():
            with self.subTest(text):
                d = self.refused(shell, text)
                again = self.judge(shell, d.instead)
                self.assertTrue(again is None or again.rule != RULE, again and again.reason)

    def test_the_instead_keeps_the_arguments(self):
        d = self.refused('ps', "kit status -C Z:/x 'x&y'")
        self.assertTrue(d.instead.endswith("/tools/kit.py\" status -C Z:/x 'x&y'"), d.instead)
        d = self.refused('ps', 'kit status $a')
        self.assertTrue(d.instead.endswith('/tools/kit.py" status <value>'), d.instead)
        d = self.refused('bash', "start //b kit status -C Z:/x 'x|y'")
        self.assertTrue(d.instead.endswith("/tools/kit.py\" status -C Z:/x 'x|y'"), d.instead)
        d = self.refused('bash', 'kit status Z:/x/*.md')
        self.assertTrue(d.instead.endswith('/tools/kit.py" status Z:/x/*.md'), d.instead)


class Chars(unittest.TestCase):
    """shell.cmd_unsafe: the characters cmd.exe reads in a batch file's arguments."""

    def test_each_character(self):
        for c in '&|<>^%"()!\r\n':
            with self.subTest(repr(c)):
                self.assertEqual(sh.cmd_unsafe('a%sb' % c), c)

    def test_delimiters_and_others_are_not(self):
        # a wildcard is not one of cmd's: an unquoted bash one is the rule's own test (bash_glob)
        for t in ('a,b;c=d', 'a b\tc', "it's", '-C', 'Z:/x\\y', 'x@y', '\uff06'):
            with self.subTest(t):
                self.assertIsNone(sh.cmd_unsafe(t))


if __name__ == '__main__':
    unittest.main()
