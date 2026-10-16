"""kitlib.guard.shell: the Bash and PowerShell tokeniser the rules read (docs/guard.md "Parsing").
Quoted text is not a command or a redirect; cd chains move the working directory; pipes,
redirects, here-docs, here-strings and command substitutions are seen; an unclosed quote marks
the parse unclean so the fallback patterns run."""
import unittest
from unittest import mock

from kitlib.config import user as kuser
from kitlib.guard import shell as s
from tests.home import fence_decoy

CWD = 'D:\\w'


def cmds(text, sh):
    p, _ = s.parse(text, sh, CWD)
    return p, p.commands


class Tokeniser(unittest.TestCase):
    def test_cd_chain_moves_the_cwd_and_pipes_are_marked(self):
        p, c = cmds('cd /d/x && python tools/gate.py --full | tail -5', s.BASH)
        self.assertEqual(p.unclean, [])
        self.assertEqual([x.argv for x in c], [['cd', '/d/x'], ['python', 'tools/gate.py', '--full'], ['tail', '-5']])
        self.assertEqual(c[1].cwd, 'D:\\x')
        self.assertTrue(c[1].piped_out)
        self.assertEqual(c[1].text, 'python tools/gate.py --full')

    def test_a_cd_behind_builtin_or_command_moves_the_cwd(self):
        for line in ('builtin cd /d/x && ls', 'command cd /d/x && ls'):
            with self.subTest(line):
                _, c = cmds(line, s.BASH)
                self.assertEqual(c[1].cwd, 'D:\\x')
        _, c = cmds('command -v cd && ls', s.BASH)
        self.assertEqual(c[1].cwd, CWD)

    def test_quoted_redirect_is_text(self):
        _, c = cmds('git commit -m "a > b" && echo hi > out.txt 2>&1', s.BASH)
        self.assertEqual(c[0].argv, ['git', 'commit', '-m', 'a > b'])
        self.assertEqual(c[0].redirects, [])
        self.assertEqual([(r.fd, r.op, r.target and r.target.text, r.dup) for r in c[1].redirects],
                         [('1', '>', 'out.txt', False), ('2', '>', None, True)])

    def test_powershell_set_location_and_pipe(self):
        _, c = cmds('Set-Location C:\\a; Get-ChildItem C:\\ -Recurse | Select-Object -First 3', s.PS)
        self.assertEqual(c[1].argv, ['Get-ChildItem', 'C:\\', '-Recurse'])
        self.assertEqual(c[1].cwd, 'C:\\a')
        self.assertTrue(c[1].piped_out)

    def test_heredoc_body_and_substitutions(self):
        _, c = cmds('cat > f <<EOF\nhello > x\nEOF\necho $(godot --headless) `ls -la`', s.BASH)
        self.assertEqual([(r.op, r.target.text) for r in c[0].redirects], [('>', 'f'), ('<<', 'hello > x')])
        nested = [x.argv for x in c if x.nested_in]
        self.assertEqual(nested, [['godot', '--headless'], ['ls', '-la']])

    def test_powershell_here_string_is_one_word(self):
        _, c = cmds("git commit -m @'\nline > x\n'@; Out-File -Encoding utf8 -FilePath 'a b.txt'", s.PS)
        self.assertEqual(c[0].argv, ['git', 'commit', '-m', 'line > x'])
        self.assertEqual(c[0].redirects, [])
        self.assertEqual(c[1].argv[-1], 'a b.txt')

    def test_escaped_semicolon_and_call_operator(self):
        _, c = cmds('find . -exec rm {} \\;', s.BASH)
        self.assertEqual(c[0].argv, ['find', '.', '-exec', 'rm', '{}', ';'])
        _, c = cmds('& "C:\\Program Files\\Godot\\godot.exe" --headless 2>&1 | Out-Null', s.PS)
        self.assertEqual(c[0].argv, ['&', 'C:\\Program Files\\Godot\\godot.exe', '--headless'])
        self.assertTrue(c[0].piped_out)

    def test_unclosed_quote_is_unclean(self):
        for text in ("echo 'unclosed", 'cmd /c "dir /s C:\\"'):
            with self.subTest(text=text):
                p, _ = cmds(text, s.BASH)
                self.assertEqual(p.unclean, ['an unclosed quote'])


ROUTED = 'C:\\routed-home'


class HomeTilde(unittest.TestCase):
    """resolve_path reads a leading `~` as the folder kitlib.config.user.home_dir() gives, so a
    guard test that reaches a `~` path with the real home meets the home fence instead of reading
    the real home (docs/test-tools.md "Home fence")."""

    def test_tilde_is_the_folder_home_dir_gives(self):
        with mock.patch.object(kuser, 'home_dir', return_value=ROUTED) as home_dir:
            for shell in (None, s.BASH, s.PS, s.CMD):
                with self.subTest(shell=shell):
                    self.assertEqual(s.resolve_path('~', CWD, shell), ROUTED)
                    self.assertEqual(s.resolve_path('~/a/b', CWD, shell), ROUTED + '\\a\\b')
                    self.assertEqual(s.resolve_path('~\\a', CWD, shell), ROUTED + '\\a')
                    self.assertIsNone(s.resolve_path('~user/a', CWD, shell))
        self.assertTrue(home_dir.called)

    def test_tilde_meets_the_fence(self):
        fence_decoy(self)
        for text in ('~', '~/a', '~\\a'):
            with self.subTest(text=text):
                with self.assertRaises(kuser.HomeFenced):
                    s.resolve_path(text, CWD)
        self.assertEqual(s.resolve_path('C:\\m\\x', CWD), 'C:\\m\\x')

    def test_a_cd_to_tilde_meets_the_fence(self):
        fence_decoy(self)
        with self.assertRaises(kuser.HomeFenced):
            cmds('cd ~/a && echo x > f', s.BASH)


if __name__ == '__main__':
    unittest.main()
