"""kitlib.allowrules: the land permission lines for one repository, the reference matcher that pins
what each line lets through, and the derivation of the kit export and main checkout they name
(docs/allow-rules.md). Fake paths only: X:/fake-export and Y:/repo, or temp folders."""
import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest

from kitlib import allowrules, pluginsource
from kitlib.allowrules import rules

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(TOP, 'tools'))
import allow_rules as cli                                           # noqa: E402
import land                                                         # noqa: E402

SHA = '0123456789ab'
K = 'X:/fake-export/' + SHA
R = 'Y:/repo'
P = 'python %s/tools/land.py' % K
Q = 'python "%s/tools/land.py"' % K
KC = 'kit land'                     # the kit command's spelling: it names no export folder
SPELLINGS = (P, Q, KC)
LINES = {'allow': rules.allow_lines(K, R), 'deny': rules.deny_lines(K, R)}
ALLOW_ONLY = {'allow': LINES['allow'], 'deny': []}
SHELLS = ('Bash', 'PowerShell')
# What the allows alone give a matching command: Bash gets no allow line (rules.ALLOW_SHELLS).
ALLOWED = {'Bash': None, 'PowerShell': 'allow'}


def submit(script=P, middle='kit/a', tail=''):
    return '%s -C %s %s --expect-tip abc123 --submit%s' % (script, R, middle, tail)


def decide(command, shell='PowerShell', lines=None):
    return rules.decide(LINES if lines is None else lines, shell, command)


class RuleTextTest(unittest.TestCase):
    def test_allow_lines_exact(self):
        want = []
        for script in ('%s/tools/land.py' % K, '"%s/tools/land.py"' % K):
            want.append('PowerShell(python %s -C Y:/repo * --expect-tip * --submit)' % script)
        want.append('PowerShell(kit land -C Y:/repo * --expect-tip * --submit)')
        self.assertEqual(LINES['allow'], want)
        self.assertEqual(LINES['allow'][0], 'PowerShell(python X:/fake-export/0123456789ab/tools/land.py'
                                            ' -C Y:/repo * --expect-tip * --submit)')

    def test_no_bash_allow_line(self):
        """Bash globs (`-[C]`, `-?` expand to a file named -C), and a deny cannot name a literal `*`, so
        no Bash command is allowed; the Bash denies stay."""
        got = rules.allow_lines(K, R)
        self.assertTrue(got)
        self.assertFalse([l for l in got if l.startswith('Bash(')])
        self.assertTrue([l for l in LINES['deny'] if l.startswith('Bash(')])
        for cmd in (submit(), submit(middle='kit/a -[C] Q:/other'), submit(middle='kit/a -? Q:/other')):
            self.assertIsNone(decide(cmd, 'Bash', ALLOW_ONLY))

    def test_one_allow_per_spelling_each_ending_at_submit(self):
        got = rules.allow_lines(K, R)
        self.assertEqual(len(got), 3)
        self.assertEqual([l.rsplit(' ', 1)[-1] for l in got], ['--submit)'] * 3)
        self.assertFalse([l for l in got if '--lane' in l])

    def test_deny_lines_exact(self):
        want = []
        for shell in SHELLS:
            for command, extra in (('python %s/tools/land.py' % K, ()), ('python "%s/tools/land.py"' % K, ()),
                                   ('kit land', ('^', '!', ')'))):
                want.append('%s(%s * -C*)' % (shell, command))
                want.append('%s(%s -C Y:/repo *-C*)' % (shell, command))
                want.append('%s(%s *--lo*)' % (shell, command))
                want.append('%s(%s * -- *)' % (shell, command))
                want += ['%s(%s *%s*)' % (shell, command, c)
                         for c in ('>', '<', '|', ';', '&', '$', '(', '`', '%', "'", '"', '\\', '{', '}', '#', '@',
                                   '\t', '\r', '\n', '\0',
                                   '‘', '’', '‚', '‛', '“', '”', '„',
                                   '–', '—', '―',
                                   '\x0b', '\x0c', '\x85', '\xa0', ' ',
                                   ' ', ' ', ' ', ' ', ' ', ' ',
                                   ' ', ' ', ' ', ' ', ' ',
                                   ' ', ' ', ' ', ' ', '　') + extra]
        self.assertEqual(LINES['deny'], want)
        self.assertEqual(len(want), 220 + 2 * 58)


class AllowedTest(unittest.TestCase):
    def test_plain_submit_allowed(self):
        for shell in SHELLS:
            self.assertEqual(decide(submit(), shell), ALLOWED[shell])

    def test_nothing_after_submit_is_allowed(self):
        for tail in (' --lane full', ' --full'):
            self.assertIsNone(decide(submit(tail=tail)))

    def test_wait_result_before_submit_allowed_and_land_takes_it(self):
        """land.py --wait-result --submit (docs/gate.md "Submitting a landing") matches the
        allow line as it is, and land.py parses it."""
        for script in SPELLINGS:
            with self.subTest(script=script):
                self.assertEqual(decide(submit(script, middle='kit/a --reviewed-by r1 --wait-result')),
                                 'allow')
        a = land.parser().parse_args(['-C', R, 'kit/a', '--expect-tip', 'abc123', '--wait-result',
                                      '--submit'])
        self.assertEqual((a.wait_result, a.submit, a.wait), (True, True, None))

    def test_batch_two_branches_allowed(self):
        self.assertEqual(decide('%s -C %s kit/a kit/b --expect-tip a1 --expect-tip b2 --summary x-and-y --submit'
                                % (P, R)), 'allow')

    def test_quoted_summary_denied(self):
        # A quote is a metacharacter: a summary with spaces is a prompt, not an allow (docs/allow-rules.md).
        self.assertEqual(decide(submit(middle='kit/a --summary "x y"')), 'deny')

    def test_quoted_kit_allowed(self):
        for shell in SHELLS:
            self.assertEqual(decide(submit(Q), shell), ALLOWED[shell])

    def test_powershell_case_insensitive(self):
        loud = submit().upper()
        self.assertEqual(decide(loud, 'PowerShell'), 'allow')
        self.assertIsNone(decide(loud, 'Bash'))

    def test_kit_command_allowed(self):
        """`kit land` runs the same land.py (the kit command): its one allow line, PowerShell only."""
        for cmd in (submit(KC), submit('Kit Land'), submit(KC).upper(),
                    '%s -C %s kit/a kit/b --expect-tip a1 --expect-tip b2 --summary x-and-y --submit' % (KC, R)):
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd), 'allow')
                self.assertIsNone(decide(cmd, 'Bash'))


class DeniedTest(unittest.TestCase):
    def test_second_C_denied(self):
        for shell in SHELLS:
            for cmd in (submit(middle='kit/a -C Q:/other'), submit(tail=' -C Q:/other'),
                        submit(Q, middle='-C Q:/other kit/a'), submit(KC, middle='kit/a -C Q:/other'),
                        submit(KC, tail=' -C Q:/other')):
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell), 'deny')

    def second_C(self, middle):
        """`middle` carries a second -C past the fixed prefix: denied in both shells and every
        spelling, though the allow alone would match it (argparse keeps the last -C)."""
        for shell in SHELLS:
            for script in SPELLINGS:
                cmd = submit(script, middle=middle)
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                    self.assertEqual(decide(cmd, shell), 'deny')

    def test_single_quoted_second_C_denied(self):
        self.second_C("kit/a '-C' Q:/other")

    def test_double_quoted_second_C_denied(self):
        self.second_C('kit/a "-C" Q:/other')

    def test_backslash_second_C_denied(self):
        self.second_C('kit/a \\-C Q:/other')

    def test_braced_second_C_denied(self):
        self.second_C('kit/a {-C,Q:/other}')

    def test_tab_second_C_denied(self):
        self.second_C('kit/a\t-C Q:/other')

    def test_any_later_C_text_denied(self):
        self.second_C('kit/a,-CQ:/other')

    def test_log_dir_denied(self):
        for middle in ('kit/a --log-dir Q:/x', 'kit/a --log-dir=Q:/x', 'kit/a --lo Q:/x', 'kit/a --log Q:/x'):
            self.only_the_deny_closes(middle)

    def only_the_deny_closes(self, middle):
        for shell in SHELLS:
            for script in (P, KC):
                cmd = submit(script, middle=middle)
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                    self.assertEqual(decide(cmd, shell), 'deny')

    def test_cmd_escape_denied_in_the_kit_spelling(self):
        """`kit` is a kit.cmd shim, and cmd.exe strips its escape `^` before python reads the line:
        `-^C` reaches land.py as a second -C past the `* -C*` deny (probed on a kit.cmd). The allow
        alone matches it; the kit spelling's `^` deny closes it."""
        for middle in ('kit/a -^C Q:/other', 'kit/a --l^og-dir Q:/x', 'kit/a -^- kit/b'):
            for shell in SHELLS:
                cmd = submit(KC, middle=middle)
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                    self.assertEqual(decide(cmd, shell), 'deny')

    def test_delayed_expansion_denied_in_the_kit_spelling(self):
        """Under delayed expansion cmd.exe expands `!name!` in kit.cmd's line, so a variable's value
        would reach land.py unread. The allow alone matches it; the kit spelling's `!` deny closes
        it. The script spellings take no `!` deny: no cmd.exe reads a python command line."""
        for shell in SHELLS:
            cmd = submit(KC, middle='kit/a !X!')
            with self.subTest(shell=shell, cmd=cmd):
                self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                self.assertEqual(decide(cmd, shell), 'deny')
        self.assertEqual(decide(submit(P, middle='kit/a !X!')), 'allow')

    def test_block_close_denied_in_the_kit_spelling(self):
        """`)` ends a ( ) block in cmd.exe; every `(` is denied already, and the kit spelling
        denies a lone `)` too. The script spellings take no `)` deny."""
        for shell in SHELLS:
            cmd = submit(KC, middle='kit/a )')
            with self.subTest(shell=shell, cmd=cmd):
                self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                self.assertEqual(decide(cmd, shell), 'deny')
        self.assertEqual(decide(submit(P, middle='kit/a )')), 'allow')

    def test_backslash_escaped_submit_denied(self):
        """`abc\\ --submit` is one word in Bash: --expect-tip takes "abc --submit" and land.py runs
        without --submit, so the allow must not carry it."""
        for shell in SHELLS:
            cmd = '%s -C %s kit/a --expect-tip abc\\ --submit' % (P, R)
            with self.subTest(shell=shell):
                self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                self.assertEqual(decide(cmd, shell), 'deny')

    def submit_dropped(self, cut):
        """`cut` before the trailing --submit hides it from land.py in PowerShell (5.1, probed): the
        line then runs a foreground landing from an agent-written --submitted payload. The allow alone
        matches it; a deny must close it."""
        cmd = '%s -C %s kit/a --submitted Q:/p.json --expect-tip abc%s --submit' % (P, R, cut)
        for shell in SHELLS:
            with self.subTest(shell=shell, cut=repr(cut)):
                self.assertEqual(decide(cmd, shell), 'deny')
        return cmd

    def test_comment_hides_submit_denied(self):
        cmd = self.submit_dropped(' #')
        self.assertEqual(cmd, 'python X:/fake-export/0123456789ab/tools/land.py -C Y:/repo kit/a'
                              ' --submitted Q:/p.json --expect-tip abc # --submit')
        self.assertEqual(decide(cmd, lines=ALLOW_ONLY), 'allow')

    def test_carriage_return_hides_submit_denied(self):
        """PowerShell ends a statement at a bare CR; the matcher does not split on it."""
        self.assertEqual(decide(self.submit_dropped('\r'), lines=ALLOW_ONLY), 'allow')

    def test_line_feed_denied(self):
        self.submit_dropped('\n')

    def test_nul_hides_submit_denied(self):
        """A native command line ends at NUL: python gets no argument after it."""
        self.assertEqual(decide(self.submit_dropped('\0'), lines=ALLOW_ONLY), 'allow')

    def test_double_dash_makes_submit_a_branch_denied(self):
        """A bare `--` inside the first `*` ends land.py's options: the trailing --submit becomes a
        branch name, so land.py runs a foreground landing, and one with --submitted before the `--`
        skips land.py's "--submit and --submitted, not both" check. The allow alone matches every
        form; the `-- ` deny closes it, and the PowerShell separators (probed 5.1: each splits a
        native argument as a space does) cannot stand in for the spaces around it."""
        tip = 'a' * 40
        forms = [('--full --expect-tip %s -- kit/a' % tip, ''),
                 ('--submitted Q:/p.land.submit --expect-tip %s -- kit/a' % tip, '')]
        for middle, tail in forms:
            argv = ['-C', R] + middle.split() + ['--expect-tip', tip, '--submit'] + tail.split()
            a = land.parser().parse_args(argv)
            self.assertFalse(a.submit, argv)
            self.assertIn('--submit', a.branches)
            for script in SPELLINGS:
                for shell in SHELLS:
                    cmd = '%s -C %s %s --expect-tip %s --submit%s' % (script, R, middle, tip, tail)
                    with self.subTest(shell=shell, cmd=cmd):
                        self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                        self.assertEqual(decide(cmd, shell), 'deny')
        for sep in rules.SEPARATORS:
            cmd = submit(middle='--full --expect-tip %s%s--%skit/a' % (tip, sep, sep))
            with self.subTest(sep='U+%04X' % ord(sep)):
                self.assertEqual(decide(cmd, lines=ALLOW_ONLY), 'allow')
                self.assertEqual(decide(cmd), 'deny')

    def test_splatting_denied(self):
        """PowerShell splats `@var` into a native command's arguments. An automatic variable needs no
        setting (`@PSVersionTable` passes `-CLRVersion:...`, which argparse takes as a second -C), and
        a variable set by an earlier approved call may carry over (`$x=,'--'`, then `@x` before the
        trailing --submit steps around the `-- ` deny; probed 5.1). The allow alone matches each; `@` is denied."""
        tip = 'a' * 40
        for middle in ('kit/a @PSVersionTable', 'kit/a @x', '--full kit/a @args',
                       '--full --expect-tip %s @x kit/a' % tip):
            self.only_the_deny_closes(middle)
            for shell in SHELLS:
                cmd = submit(Q, middle=middle)
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])
                    self.assertEqual(decide(cmd, shell), 'deny')

    def test_typographic_quotes_split_C_and_log_dir_denied(self):
        """PowerShell takes U+2018-U+201B as single and U+201C-U+201E as double quotes and strips
        them from a native argument (probed): `-‘C’` reaches land.py as -C, `--l‘o’g-dir` as --log-dir."""
        for q in '\u2018\u2019\u201a\u201b\u201c\u201d\u201e':
            self.second_C('kit/a -%sC%s Q:/other' % (q, q))
            self.only_the_deny_closes('kit/a --l%so%sg-dir Q:/x' % (q, q))

    def test_typographic_dashes_denied(self):
        """PowerShell takes U+2013-U+2015 as parameter dashes. Probed 5.1 passes them to python as
        written, so `–C` is no -C there; denied anyway, as no submit needs one."""
        for d in '\u2013\u2014\u2015':
            self.second_C('kit/a %sC Q:/other' % d)
            self.only_the_deny_closes('kit/a %s%slog-dir Q:/x' % (d, d))

    def test_glued_C_denied(self):
        self.assertEqual(decide(submit(middle='kit/a -CQ:/other')), 'deny')

    def test_first_C_not_denied(self):
        deny_only = {'allow': [], 'deny': LINES['deny']}
        for shell in SHELLS:
            for cmd in (submit(), submit(Q), submit(middle='kit/a--b'), submit(KC), submit(KC, middle='kit/a--b')):
                self.assertIsNone(decide(cmd, shell, deny_only))

    def test_two_spaces_before_first_C_denied_by_the_reference(self):
        # The reference matcher's reading; how Claude Code treats runs of spaces is INFERENCE.
        self.assertEqual(decide(submit().replace('land.py -C', 'land.py  -C')), 'deny')

    def test_redirect_denied(self):
        for cmd in (submit(tail=' > Q:/x'), submit(tail=' 2>Q:/x')):
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd), 'deny')
                self.assertIsNone(decide(cmd, lines=ALLOW_ONLY))  # F2: the fixed tail takes no redirect
        inner = submit(middle='kit/a < Q:/in')
        self.assertEqual(decide(inner), 'deny')
        self.assertEqual(decide(inner, lines=ALLOW_ONLY), 'allow')     # the first `*` carries it: the deny closes it

    def test_semicolon_denied(self):
        for shell in SHELLS:
            self.assertEqual(decide(submit(tail='; git push --force'), shell), 'deny')
            self.assertEqual(decide(submit(middle='kit/a;x'), shell), 'deny')

    def test_substitution_denied(self):
        for cmd in (submit(middle='$(evil) kit/a'), submit(middle='`evil` kit/a'), submit(middle='kit/$x'),
                    submit(middle='(evil) kit/a'), submit(middle='@(evil) kit/a'), submit(middle='%X% kit/a')):
            for shell in SHELLS:
                with self.subTest(cmd=cmd, shell=shell):
                    self.assertEqual(decide(cmd, shell), 'deny')
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), ALLOWED[shell])   # the hole the deny closes

    def test_every_metachar_in_an_allowed_command_is_denied(self):
        bases = [submit(), submit(Q), submit(KC)]
        for c in rules.METACHARS:
            for base in bases:
                cmd = base.replace(' kit/a ', ' kit/a%sb ' % c)
                for shell in SHELLS:
                    with self.subTest(c=c, cmd=cmd, shell=shell):
                        self.assertEqual(decide(cmd, shell), 'deny')

    def test_abbreviation_matches_the_rule_and_land_refuses_it(self):
        """An argparse prefix inside the first `*` is no hole: every land.py option it could name is
        one land.py refuses beside --submit (arguments and exit codes only)."""
        for extra in ('--sw', '--sweep', '--submitte P', '--subm', '--qu', '--app'):
            cmd = submit(middle='kit/a %s' % extra)
            with self.subTest(extra=extra):
                self.assertEqual(decide(cmd), 'allow')
                argv = ['-C', 'Y:/repo', 'kit/a'] + extra.split() + ['--expect-tip', 'abc123', '--submit']
                with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as e:
                        land.main(argv)
                self.assertEqual(e.exception.code, 2)


class NoMatchTest(unittest.TestCase):
    def test_R_evil_no_match(self):
        for cmd in (submit().replace('-C Y:/repo ', '-C Y:/repo-evil '),
                    submit().replace('-C Y:/repo ', '-C Y:/repo/../x ')):
            self.assertIsNone(decide(cmd))

    def test_repo_own_land_no_match(self):
        self.assertIsNone(decide(submit('python Y:/repo/tools/land.py')))

    def test_other_export_no_match(self):
        self.assertIsNone(decide(submit('python X:/fake-export/ba9876543210/tools/land.py')))

    def test_other_tool_no_match(self):
        self.assertIsNone(decide(submit(P.replace('land.py', 'gate.py'))))
        self.assertIsNone(decide(submit(P.replace('python ', 'py '))))

    def test_other_kit_spelling_no_match(self):
        """Only `kit land` is allowed: another spelling of the kit command, or another of its tools,
        gets the normal prompt."""
        for script in ('kit.cmd land', 'kit gate', 'kit  land', 'kit landx', 'python -m tools.kit land',
                       'python %s/tools/kit.py land' % K, 'Y:/repo/kit land'):
            with self.subTest(script=script):
                self.assertIsNone(decide(submit(script)))

    def test_kit_R_evil_no_match(self):
        for cmd in (submit(KC).replace('-C Y:/repo ', '-C Y:/repo-evil '),
                    submit(KC).replace('-C Y:/repo ', '-C Y:/repo/../x ')):
            self.assertIsNone(decide(cmd))

    def test_chain_part_unmatched(self):
        for tail in (' && git push --force', '; git push --force', ' | tee x', ' |& tee x', '\ngit push --force',
                     ' & git push --force', ' || git push --force'):
            for shell in SHELLS:
                with self.subTest(tail=tail, shell=shell):
                    self.assertNotEqual(decide(submit(tail=tail), shell), 'allow')
                    self.assertNotEqual(decide(submit(tail=tail), shell, ALLOW_ONLY), 'allow')

    def test_quoted_or_backslash_R_no_match(self):
        for cmd in (submit().replace('-C Y:/repo ', '-C "Y:/repo" '), submit().replace('-C Y:/repo ', '-C Y:\\repo ')):
            self.assertIsNone(decide(cmd, lines=ALLOW_ONLY))
            self.assertEqual(decide(cmd), 'deny')               # the quote and backslash denies

    def test_other_shell_rules_ignored(self):
        self.assertIsNone(decide(submit(), 'Read'))


def make_export(root, sha, marker=True):
    os.makedirs(os.path.join(root, sha), exist_ok=True)
    if marker:
        with open(os.path.join(root, sha, pluginsource.MARKER), 'w', encoding='utf-8') as fh:
            json.dump({'sha': 'x'}, fh)
    pluginsource.write_marketplace(root, sha)


def settings(root):
    return pluginsource.Settings(root, False, 'origin')


class DeriveTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = os.path.join(d.name, 'export')
        self.seen = []

    def top(self, path):
        self.seen.append(path)
        return 'Y:\\repo'

    def derive(self, root=None, top=None):
        return allowrules.derive('Y:/repo/.claude/worktrees/w', settings=settings(root or self.root),
                                 toplevel=top or self.top)

    def test_derive_from_fake_export(self):
        make_export(self.root, SHA)
        t = self.derive()
        self.assertEqual(t.kit, (os.path.normpath(os.path.join(self.root, SHA))).replace('\\', '/'))
        self.assertEqual((t.repo, t.sha), ('Y:/repo', SHA))
        self.assertEqual(self.seen, ['Y:/repo/.claude/worktrees/w'])
        got = allowrules.lines(t)
        self.assertEqual(got, {'allow': rules.allow_lines(t.kit, 'Y:/repo'),
                               'deny': rules.deny_lines(t.kit, 'Y:/repo')})

    def test_no_hard_coded_hash(self):
        other = os.path.join(os.path.dirname(self.root), 'export2')
        make_export(self.root, SHA)
        make_export(other, 'fedcba987654')
        a, b = allowrules.lines(self.derive()), allowrules.lines(self.derive(other))
        for got, sha in ((a, SHA), (b, 'fedcba987654')):
            script = [l for l in got['allow'] + got['deny'] if '(kit land ' not in l]
            kit = [l for l in got['allow'] + got['deny'] if '(kit land ' in l]
            self.assertTrue(script and kit)
            self.assertTrue(all(sha in l for l in script))      # the script lines name the export
            self.assertFalse([l for l in kit if sha in l])      # the kit command's name none
        src = os.path.join(TOP, 'kitlib', 'allowrules')
        for name in os.listdir(src):
            if name.endswith('.py'):
                with open(os.path.join(src, name), encoding='utf-8') as fh:
                    self.assertFalse(re.findall(r'(?<![0-9a-zA-Z])[0-9a-f]{12}(?![0-9a-zA-Z])', fh.read()), name)

    def test_refuses_no_current_export(self):
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'no current plugin export'):
            self.derive()
        os.makedirs(os.path.join(self.root, 'not-a-sha'))
        pluginsource.write_marketplace(self.root, 'not-a-sha')
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'no current plugin export'):
            self.derive()

    def test_refuses_missing_marker(self):
        make_export(self.root, SHA, marker=False)
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'no current plugin export.*marker'):
            self.derive()

    def test_refuses_metachar_path(self):
        make_export(self.root, SHA)
        for c in ' \t"\'()*?[];&|$`%<>':
            with self.subTest(c=repr(c)):
                with self.assertRaisesRegex(allowrules.AllowRulesError, 'main checkout'):
                    self.derive(top=lambda p: 'Y:/re%spo' % c)

    def test_refuses_control_character_path(self):
        make_export(self.root, SHA)
        for c in [chr(i) for i in range(32)] + ['\x7f']:
            with self.subTest(c=repr(c)):
                with self.assertRaisesRegex(allowrules.AllowRulesError, 'main checkout'):
                    self.derive(top=lambda p: 'Y:/re%spo' % c)

    def test_refuses_path_a_deny_names(self):
        """A path holding a character a deny line names would be denied by that line on every submit."""
        make_export(self.root, SHA)
        for c in rules.METACHARS:
            if c == '\\':
                continue                    # a backslash becomes `/` in the path the rules name
            with self.subTest(c=repr(c)):
                with self.assertRaisesRegex(allowrules.AllowRulesError, 'main checkout'):
                    self.derive(top=lambda p: 'Y:/re%spo' % c)

    def test_refuses_kit_path_with_a_space(self):
        spaced = os.path.join(os.path.dirname(self.root), 'ex port')
        make_export(spaced, SHA)
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'export'):
            self.derive(spaced)

    def test_refuses_unplaced_checkout(self):
        make_export(self.root, SHA)
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'repository'):
            self.derive(top=lambda p: None)

    def test_the_repositorys_config_is_not_read(self):
        make_export(self.root, SHA)
        with tempfile.TemporaryDirectory() as top:
            with open(os.path.join(top, '.kit.json'), 'w', encoding='utf-8') as fh:
                fh.write('{ not json')
            t = allowrules.derive(top, settings=settings(self.root), toplevel=lambda p: top)
        self.assertEqual(t.repo, os.path.normpath(top).replace('\\', '/'))


def target():
    return allowrules.Target('X:/fake-export/' + SHA, 'Y:/repo', SHA, 'X:/fake-export')


class CliTest(unittest.TestCase):
    def run_cli(self, argv, derive):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv, derive=derive)
        return code, out.getvalue(), err.getvalue()

    def test_cli_json_and_exit(self):
        seen = []

        def fake(checkout):
            seen.append(checkout)
            return target()
        code, out, err = self.run_cli(['-C', 'Y:/repo/wt'], fake)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {'permissions': allowrules.lines(target())})
        self.assertEqual(seen, ['Y:/repo/wt'])
        self.assertIn('target: Y:/repo/.claude/settings.local.json', err)
        self.assertIn('export: ' + SHA, err)

    def test_cli_refusal_exit_2(self):
        def fake(checkout):
            raise allowrules.AllowRulesError('no current plugin export under X:/fake-export (none)')
        code, out, err = self.run_cli([], fake)
        self.assertEqual(code, 2)
        self.assertIn('REFUSED: no current plugin export under X:/fake-export (none)', err)
        self.assertEqual(out, '')

    def test_cli_bad_argument_exit_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                cli.main(['--bogus'], derive=lambda c: target())
        self.assertEqual(e.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
