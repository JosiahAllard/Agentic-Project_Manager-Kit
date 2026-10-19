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
from kitlib.config.types import ConfigError

TOP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(TOP, 'tools'))
import allow_rules as cli                                           # noqa: E402
import land                                                         # noqa: E402

SHA = '0123456789ab'
K = 'X:/fake-export/' + SHA
R = 'Y:/repo'
P = 'python %s/tools/land.py' % K
Q = 'python "%s/tools/land.py"' % K
LINES = {'allow': rules.allow_lines(K, R, ('full',)), 'deny': rules.deny_lines(K, R)}
ALLOW_ONLY = {'allow': LINES['allow'], 'deny': []}
SHELLS = ('Bash', 'PowerShell')


def submit(script=P, middle='kit/a', tail=''):
    return '%s -C %s %s --expect-tip abc123 --submit%s' % (script, R, middle, tail)


def decide(command, shell='Bash', lines=None):
    return rules.decide(LINES if lines is None else lines, shell, command)


class RuleTextTest(unittest.TestCase):
    def test_allow_lines_exact(self):
        want = []
        for shell in SHELLS:
            for script in ('%s/tools/land.py' % K, '"%s/tools/land.py"' % K):
                want += ['%s(python %s -C Y:/repo * --expect-tip * --submit)' % (shell, script),
                         '%s(python %s -C Y:/repo * --expect-tip * --submit --lane full)' % (shell, script)]
        self.assertEqual(LINES['allow'], want)
        self.assertEqual(LINES['allow'][0],
                         'Bash(python X:/fake-export/0123456789ab/tools/land.py -C Y:/repo * --expect-tip * --submit)')

    def test_one_allow_per_lane_full_first_never_a_lane_star(self):
        got = rules.allow_lines(K, R, ('full', 'quick', 'docs'))
        self.assertEqual(len(got), 2 * 2 * 4)
        self.assertEqual([l.rsplit(' ', 1)[-1] for l in got[:4]], ['--submit)', 'full)', 'quick)', 'docs)'])
        self.assertFalse([l for l in got if '--lane *' in l])

    def test_deny_lines_exact(self):
        want = []
        for shell in SHELLS:
            for script in ('%s/tools/land.py' % K, '"%s/tools/land.py"' % K):
                want.append('%s(python %s * -C*)' % (shell, script))
                want.append('%s(python %s -C Y:/repo *-C*)' % (shell, script))
                want.append('%s(python %s *--lo*)' % (shell, script))
                want += ['%s(python %s *%s*)' % (shell, script, c)
                         for c in ('>', '<', '|', ';', '&', '$', '(', '`', '%', "'", '"', '\\', '{', '}', '\t')]
        self.assertEqual(LINES['deny'], want)
        self.assertEqual(len(want), 72)


class AllowedTest(unittest.TestCase):
    def test_plain_submit_allowed(self):
        for shell in SHELLS:
            self.assertEqual(decide(submit(), shell), 'allow')

    def test_lane_allowed(self):
        self.assertEqual(decide(submit(tail=' --lane full')), 'allow')
        self.assertIsNone(decide(submit(tail=' --lane quick')))      # not a lane of this repo
        lanes = {'allow': rules.allow_lines(K, R, ('full', 'quick')), 'deny': []}
        self.assertEqual(decide(submit(tail=' --lane quick'), lines=lanes), 'allow')

    def test_batch_two_branches_allowed(self):
        self.assertEqual(decide('%s -C %s kit/a kit/b --expect-tip a1 --expect-tip b2 --summary x-and-y --submit'
                                % (P, R)), 'allow')

    def test_quoted_summary_denied(self):
        # A quote is a metacharacter: a summary with spaces is a prompt, not an allow (docs/allow-rules.md).
        self.assertEqual(decide(submit(middle='kit/a --summary "x y"')), 'deny')

    def test_quoted_kit_allowed(self):
        for shell in SHELLS:
            self.assertEqual(decide(submit(Q), shell), 'allow')

    def test_powershell_case_insensitive(self):
        loud = submit().upper()
        self.assertEqual(decide(loud, 'PowerShell'), 'allow')
        self.assertIsNone(decide(loud, 'Bash'))


class DeniedTest(unittest.TestCase):
    def test_second_C_denied(self):
        for shell in SHELLS:
            for cmd in (submit(middle='kit/a -C Z:/other'), submit(tail=' -C Z:/other'),
                        submit(tail=' --lane full -C Z:/other'), submit(Q, middle='-C Z:/other kit/a')):
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell), 'deny')

    def second_C(self, middle):
        """`middle` carries a second -C past the fixed prefix: denied in both shells and both script
        spellings, though the allow alone would match it (argparse keeps the last -C)."""
        for shell in SHELLS:
            for script in (P, Q):
                cmd = submit(script, middle=middle)
                with self.subTest(shell=shell, cmd=cmd):
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), 'allow')
                    self.assertEqual(decide(cmd, shell), 'deny')

    def test_single_quoted_second_C_denied(self):
        self.second_C("kit/a '-C' Z:/other")

    def test_double_quoted_second_C_denied(self):
        self.second_C('kit/a "-C" Z:/other')

    def test_backslash_second_C_denied(self):
        self.second_C('kit/a \\-C Z:/other')

    def test_braced_second_C_denied(self):
        self.second_C('kit/a {-C,Z:/other}')

    def test_tab_second_C_denied(self):
        self.second_C('kit/a\t-C Z:/other')

    def test_any_later_C_text_denied(self):
        self.second_C('kit/a,-CZ:/other')

    def test_log_dir_denied(self):
        for middle in ('kit/a --log-dir Z:/x', 'kit/a --log-dir=Z:/x', 'kit/a --lo Z:/x', 'kit/a --log Z:/x'):
            self.only_the_deny_closes(middle)

    def only_the_deny_closes(self, middle):
        for shell in SHELLS:
            cmd = submit(middle=middle)
            with self.subTest(shell=shell, cmd=cmd):
                self.assertEqual(decide(cmd, shell, ALLOW_ONLY), 'allow')
                self.assertEqual(decide(cmd, shell), 'deny')

    def test_backslash_escaped_submit_denied(self):
        """`abc\\ --submit` is one word in Bash: --expect-tip takes "abc --submit" and land.py runs
        without --submit, so the allow must not carry it."""
        for shell in SHELLS:
            cmd = '%s -C %s kit/a --expect-tip abc\\ --submit' % (P, R)
            with self.subTest(shell=shell):
                self.assertEqual(decide(cmd, shell, ALLOW_ONLY), 'allow')
                self.assertEqual(decide(cmd, shell), 'deny')

    def test_glued_C_denied(self):
        self.assertEqual(decide(submit(middle='kit/a -CZ:/other')), 'deny')

    def test_first_C_not_denied(self):
        deny_only = {'allow': [], 'deny': LINES['deny']}
        for shell in SHELLS:
            for cmd in (submit(), submit(Q), submit(tail=' --lane full')):
                self.assertIsNone(decide(cmd, shell, deny_only))

    def test_two_spaces_before_first_C_denied_by_the_reference(self):
        # The reference matcher's reading; how Claude Code treats runs of spaces is INFERENCE.
        self.assertEqual(decide(submit().replace('land.py -C', 'land.py  -C')), 'deny')

    def test_redirect_denied(self):
        for cmd in (submit(tail=' > Z:/x'), submit(tail=' --lane full > Z:/x'), submit(tail=' 2>Z:/x')):
            with self.subTest(cmd=cmd):
                self.assertEqual(decide(cmd), 'deny')
                self.assertIsNone(decide(cmd, lines=ALLOW_ONLY))  # F2: a fixed lane tail takes no redirect
        inner = submit(middle='kit/a < Z:/in')
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
                    self.assertEqual(decide(cmd, shell, ALLOW_ONLY), 'allow')   # the hole the deny closes

    def test_every_metachar_in_an_allowed_command_is_denied(self):
        bases = [submit(), submit(Q), submit(tail=' --lane full')]
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

    def derive(self, root=None, lanes=None, top=None):
        return allowrules.derive('Y:/repo/.claude/worktrees/w', settings=settings(root or self.root),
                                 toplevel=top or self.top, lanes_of=lambda t: lanes or {})

    def test_derive_from_fake_export(self):
        make_export(self.root, SHA)
        t = self.derive(lanes={'quick': {}})
        self.assertEqual(t.kit, (os.path.normpath(os.path.join(self.root, SHA))).replace('\\', '/'))
        self.assertEqual((t.repo, t.sha, t.lanes), ('Y:/repo', SHA, ('full', 'quick')))
        self.assertEqual(self.seen, ['Y:/repo/.claude/worktrees/w'])
        got = allowrules.lines(t)
        self.assertEqual(got, {'allow': rules.allow_lines(t.kit, 'Y:/repo', ('full', 'quick')),
                               'deny': rules.deny_lines(t.kit, 'Y:/repo')})

    def test_no_hard_coded_hash(self):
        other = os.path.join(os.path.dirname(self.root), 'export2')
        make_export(self.root, SHA)
        make_export(other, 'fedcba987654')
        a, b = allowrules.lines(self.derive()), allowrules.lines(self.derive(other))
        self.assertTrue(all(SHA in l for l in a['allow'] + a['deny']))
        self.assertTrue(all('fedcba987654' in l for l in b['allow'] + b['deny']))
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

    def test_refuses_kit_path_with_a_space(self):
        spaced = os.path.join(os.path.dirname(self.root), 'ex port')
        make_export(spaced, SHA)
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'export'):
            self.derive(spaced)

    def test_refuses_unplaced_checkout(self):
        make_export(self.root, SHA)
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'repository'):
            self.derive(top=lambda p: None)

    def test_refuses_bad_lane_name(self):
        make_export(self.root, SHA)
        for bad in ('a b', '-x', 'a*', 'a;b', ''):
            with self.subTest(lane=bad):
                with self.assertRaisesRegex(allowrules.AllowRulesError, 'lane'):
                    self.derive(lanes={bad: {}})

    def test_refuses_unreadable_config(self):
        make_export(self.root, SHA)

        def broken(top):
            raise ConfigError('bad .kit.json')
        with self.assertRaisesRegex(allowrules.AllowRulesError, 'bad .kit.json'):
            allowrules.derive('Y:/repo', settings=settings(self.root), toplevel=self.top, lanes_of=broken)


def target():
    return allowrules.Target('X:/fake-export/' + SHA, 'Y:/repo', SHA, ('full',), 'X:/fake-export')


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
