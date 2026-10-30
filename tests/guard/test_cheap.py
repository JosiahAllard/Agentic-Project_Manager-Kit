"""The cheap guard: rule modes (kitlib.guard.modes) and how a warned call reaches
the agent, the rules that now warn or stop, and the denies it keeps."""
import json
import os
import unittest
from unittest import mock

from hosts.claude.adapter import host as claude_host
from hosts.codex.adapter.host import ADAPTER as CODEX
from kitlib import config, pmdata
from kitlib.config import schema, user
from kitlib.guard import modes, rules
from kitlib.host import Action, Decision, hookmain

from . import world as W
from .claudehost import action, output
from .world import event
from kitlib import guard


def write_ev(target, cwd, sub=True):
    return event('Write', {'file_path': target, 'content': 'x'}, cwd, sub)


class World(unittest.TestCase):
    def setUp(self):
        self.w = W.World()
        self.addCleanup(self.w.cleanup)

    def decide(self, payload, **kw):
        return guard.decide(action(payload), self.w.ctx(**kw))


class Modes(World):
    """A rule's mode: deny by default, the owner's machine config over the kit's defaults."""

    def main_write(self):
        return write_ev(os.path.join(self.w.main, 'README.md'), self.w.wt)

    def test_a_rule_the_defaults_do_not_name_is_deny(self):
        self.assertEqual(modes.mode('no-such-rule'), modes.DENY)
        d = self.decide(self.main_write())
        self.assertEqual((d.verdict, d.rule), ('deny', 'main-checkout'))

    def test_warn_allows_with_the_refusal_as_a_warning(self):
        self.w.user_config({'guard_rules': {'main-checkout': 'warn', 'write-jail': 'off'}})
        d = self.decide(self.main_write())
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED))
        self.assertTrue(d.reason.startswith(rules.WARNING), d.reason)
        self.assertIn('rule=main-checkout', d.reason)

    def test_off_allows_and_says_nothing(self):
        self.w.user_config({'guard_rules': {'main-checkout': 'off', 'write-jail': 'off'}})
        self.assertEqual(self.decide(self.main_write()), Decision('allow'))

    def test_deny_in_the_config_restores_a_default(self):
        with mock.patch.dict(modes.DEFAULTS, {'main-checkout': modes.WARN, 'write-jail': modes.OFF}):
            self.assertEqual(self.decide(self.main_write()).verdict, 'allow')
            self.w.user_config({'guard_rules': {'main-checkout': 'deny'}})
            self.assertEqual(self.decide(self.main_write()).verdict, 'deny')

    def test_a_config_the_strict_read_refuses_leaves_the_defaults(self):
        self.w.user_config({'guard_rules': {'main-checkout': 'maybe'}})
        self.assertEqual(self.decide(self.main_write()).verdict, 'deny')

    def test_the_modes_hold_for_one_call_only(self):
        self.w.user_config({'guard_rules': {'main-checkout': 'warn', 'write-jail': 'off'}})
        self.decide(self.main_write())
        self.assertEqual(modes.mode('main-checkout'), modes.DENY)

    def test_resolve_reads_the_machine_config_only(self):
        class Ctx(object):
            def user(self):
                return {'guard_rules': {'heavy': 'off', 'x': 'loud', 7: 'warn'}}
        out = modes.resolve(Ctx())
        self.assertEqual(out['heavy'], modes.OFF)
        self.assertNotIn('x', out)
        self.assertNotIn(7, out)

        class Broken(object):
            def user(self):
                raise OSError('unreadable')
        self.assertEqual(modes.resolve(Broken()), modes.DEFAULTS)

    def test_no_kit_json_section_takes_the_modes(self):
        self.assertIn('guard_rules', user.SCHEMA)
        self.assertFalse(any('guard_rules' in fields or 'rules' in fields for fields in schema.SECTIONS.values()))


class Unplaced(World):
    """Item 1: a write the guard cannot place warns (rule unplaced); one that resolves literally into
    a main checkout or outside an agent session's folders is still denied."""

    def warned(self, payload, **kw):
        d = self.decide(payload, **kw)
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
        self.assertIn('rule=unplaced', d.reason)
        return d

    def test_a_computed_redirect_target_warns(self):
        self.warned(W.bash('echo x > "$OUT"', self.w.main, sub=True))
        self.warned(W.ps('Set-Content -Path $p -Value x', self.w.main, sub=True))

    def test_python_c_and_node_e_in_a_main_checkout_warn(self):
        self.warned(W.bash('python -c "open(\'x.txt\', \'w\').write(\'y\')"', self.w.main, sub=True))
        self.warned(W.bash('node -e "require(\'fs\').writeFileSync(\'x\', \'y\')"', self.w.main, sub=True))

    def test_a_warning_quotes_at_most_120_characters_of_the_command_and_a_refusal_all_of_it(self):
        code = '; '.join("open('x%d.txt', 'w').write('y')" % i for i in range(20))
        command = 'python -c "%s"' % code
        self.assertGreater(len(command), 600)
        warned = self.warned(W.bash(command, self.w.main, sub=True)).reason
        self.assertNotIn('`%s`' % command, warned)
        self.assertIn('`%s%s`' % (command[:rules.CITE_MAX], rules.ELIDED), warned)
        self.w.user_config({'guard_rules': {'unplaced': 'deny'}})
        denied = self.decide(W.bash(command, self.w.main, sub=True))
        self.assertEqual((denied.verdict, denied.rule), ('deny', 'unplaced'), denied.reason)
        self.assertIn('`%s`' % command, denied.reason)                   # a refusal keeps its full text
        self.assertIn(' Instead: %s %s' % (denied.instead, rules.tag('unplaced', 'subagent')), warned)

    def test_cited_cuts_only_a_long_quote_of_the_command_and_reads_past_its_backticks(self):
        cmd = 'Set-Content -Path x.txt -Value "a`nb" ' + 'y' * 200
        what = '`%s` writes x.txt; see `Set-Content`' % cmd
        self.assertEqual(rules.cited(what, cmd),
                         '`%s%s` writes x.txt; see `Set-Content`' % (cmd[:rules.CITE_MAX], rules.ELIDED))
        self.assertEqual(rules.cited(what, None), what)
        short = '`echo hi` writes x'
        self.assertEqual(rules.cited(short, 'echo hi'), short)
        other = '`%s`' % ('z' * 200)                                     # not the command's text
        self.assertEqual(rules.cited(other, cmd), other)

    def test_git_with_a_computed_dir_warns(self):
        self.warned(W.bash('git -C "$d" commit -m x', self.w.wt, sub=True))

    def test_a_literal_write_into_a_main_checkout_is_denied(self):
        d = self.decide(W.bash('echo x > %s' % W.fwd(os.path.join(self.w.main, 'a.txt')), self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'main-checkout'))
        d = self.decide(W.bash('git -C %s commit -m x' % W.fwd(self.w.main), self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-main-history'))

    def test_a_literal_write_outside_the_folders_is_denied(self):
        d = self.decide(W.bash('echo x > %s' % W.fwd(os.path.join(self.w.plain, 'a.txt')), self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'))

    def test_the_owner_can_make_unplaced_deny(self):
        self.w.user_config({'guard_rules': {'unplaced': 'deny'}})
        d = self.decide(W.bash('echo x > "$OUT"', self.w.main, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'unplaced'))
        self.assertIn('rule=unplaced', d.reason)
        self.assertNotIn('rule=main-checkout', d.reason)

    def test_a_place_git_cannot_tell_warns_but_a_git_directory_is_the_checkout(self):
        slow = os.path.join(self.w.wt, 'slow')

        def classify(path):                     # git times out on wt/slow; no work tree inside .git
            p = os.path.normcase(os.path.abspath(path))
            if p.startswith(os.path.normcase(slow)):
                return W.repo.Place(W.repo.UNKNOWN, None, 'git did not answer: timed out', None, None)
            if '.git' in p.split(os.sep):
                return W.repo.Place(W.repo.UNKNOWN, None, 'fatal: this operation must be run in a work tree',
                                    None, None)
            return self.w.classify(path)
        self.warned(write_ev(os.path.join(slow, 'a.txt'), self.w.wt), classify=classify)
        d = self.decide(write_ev(os.path.join(self.w.main, '.git', 'config'), self.w.main), classify=classify)
        self.assertEqual((d.verdict, d.rule), ('deny', 'main-checkout'))


class KeptDenies(World):
    """Denies the cheap guard keeps or adds: the gate result folder (new) and the machine config."""

    def test_a_literal_write_into_the_gate_result_folder_is_denied(self):
        rec = os.path.join(self.w.main, '.git', 'kit-gate-result', 'kit%2Fx.gate.json')
        for payload in (write_ev(rec, self.w.wt),
                        W.bash('echo {} > %s' % W.fwd(rec), self.w.wt, sub=True),
                        write_ev(rec, self.w.main, sub=False)):
            d = self.decide(payload, role_resolver=lambda *a: 'coordinator')
            self.assertEqual((d.verdict, d.rule), ('deny', 'gate-result'), payload)
            self.assertIn('wait-result', d.instead)

    def test_a_folder_of_that_name_outside_a_git_directory_is_not_it(self):
        target = os.path.join(self.w.wt, 'docs', 'kit-gate-result', 'x.md')
        self.assertEqual(self.decide(write_ev(target, self.w.wt)), Decision('allow'))

    def test_the_machine_config_stays_denied_to_an_agent(self):
        cfg = user.config_file(self.w.home)
        d = self.decide(write_ev(cfg, self.w.wt))
        self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'))
        d = self.decide(W.bash('echo {} > %s' % W.fwd(cfg), self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'))

    def test_the_owners_delegation_list_is_a_kept_deny(self):
        """owner-delegations is not softened (no modes.DEFAULTS entry), so a write of the list is
        denied, a plain-marked session's included."""
        self.assertNotIn('owner-delegations', modes.DEFAULTS)
        self.assertEqual(modes.mode('owner-delegations'), modes.DENY)
        target = os.path.join(self.w.home, '.pm-kit', 'owner-delegations.json')
        for payload, kw in ((write_ev(target, self.w.wt), {}),
                            (W.bash('rm %s' % W.fwd(target), self.w.wt, sub=False),
                             {'role_resolver': lambda *a: 'plain'})):
            d = self.decide(payload, pointer=lambda: None, **kw)
            self.assertEqual((d.verdict, d.rule), ('deny', 'owner-delegations'), d.reason)


class StrictGitReads(World):
    """Where the checkout cannot be placed (git -C $x), git_reads(strict) keeps each form below a
    non-read exactly as before the cheap guard: it is refused, under unplaced (held at deny here;
    its default warns) where it was write-jail, and a PowerShell splat stays write-jail. A plain
    read, and a computed word strict_dyn_read allows, is let through as before. grep -O is a read
    and symbolic-ref with one ref is one (only an attacker runs a program through -O;
    archive --remote/--exec: test_readonly_git_help)."""

    NON_READS = (
        'log $f', 'show "$f"', 'diff $a $b',                     # computed words strict_dyn_read refuses
        'log --output=f', 'show --outp=f', 'diff --output f',    # writes_output_file
        'branch --list --unset-up', 'tag -l --del', 'config --get --repl a.b c')   # strict_read_opts
    READS = ('log', 'show HEAD', 'diff', 'grep -n foo', 'branch -vv', 'tag -l', 'config --get a.b',
             'config -f x.cfg --get a.b',
             'grep -O foo', 'grep -nO foo', 'grep --open foo',
             'symbolic-ref --short refs/remotes/origin/HEAD',
             'log "HEAD~$n"', 'log --format="$f"', 'rev-parse $f')   # dyn_head / strict_dyn_read

    def git(self, rest, shell=W.bash):
        return self.decide(shell('git -C $x %s' % rest, self.w.wt, sub=True))

    def test_each_non_read_is_refused(self):
        W.deny_unplaced()
        self.addCleanup(W.allow_unplaced)
        for rest in self.NON_READS:
            d = self.git(rest)
            self.assertEqual((d.verdict, d.rule), ('deny', 'unplaced'), rest)
        d = self.git('log $f', W.ps)
        self.assertEqual((d.verdict, d.rule), ('deny', 'unplaced'))

    def test_by_default_each_non_read_warns(self):
        for rest in self.NON_READS:
            d = self.git(rest)
            self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), rest)
            self.assertIn('rule=unplaced', d.reason)

    def test_a_powershell_splat_is_a_computed_word(self):
        # the splat may hold any option, as $f may: by default it warns as unplaced
        d = self.git('log @f', W.ps)
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED))
        self.assertIn('rule=unplaced', d.reason)
        self.assertEqual(self.git("log '@f'", W.ps), Decision('allow'))    # quoted: plain text

    def test_a_plain_read_is_let_through(self):
        W.deny_unplaced()
        self.addCleanup(W.allow_unplaced)
        for rest in self.READS:
            self.assertEqual(self.git(rest), Decision('allow'), rest)
        self.assertEqual(self.git('log "HEAD~$n"', W.ps), Decision('allow'))


class Dropped(World):
    """Item 2: wrapped-gate and busy-wait are off; unreadable and git-alias only warn; the kept
    rules still judge such a line; the owner's machine config turns each back on. hub-owner-verb on
    a parsed run stays deny."""

    def wrapped_gate(self):
        return W.bash('python tools/gate.py --full 2>&1 | tail -5', self.w.wt, sub=True)

    def busy_wait(self):
        return W.bash('while ! test -f done.txt; do sleep 5; done', self.w.wt, sub=True)

    def unreadable(self):
        return W.bash("echo 'x ; git merge kit/wt", self.w.main)

    def alias(self):
        return W.bash('git -c alias.co=checkout co main', self.w.wt, sub=True)

    def test_the_kit_defaults(self):
        for rule, m in (('wrapped-gate', modes.OFF), ('busy-wait', modes.OFF),
                        ('unreadable', modes.WARN), ('git-alias', modes.WARN)):
            with self.subTest(rule):
                self.assertEqual(modes.DEFAULTS.get(rule), m)
        self.assertNotIn('hub-owner-verb', modes.DEFAULTS)
        self.assertEqual(modes.mode('hub-owner-verb'), modes.DENY)

    def test_an_off_rule_lets_the_call_run_and_says_nothing(self):
        self.w.git.ancestors.add((W.SHA_WT, 'main'))
        for name, make in (('wrapped-gate', self.wrapped_gate), ('busy-wait', self.busy_wait)):
            with self.subTest(name):
                self.assertEqual(self.decide(make()), Decision('allow'))

    def test_a_warned_rule_lets_the_call_run_with_its_warning(self):
        for name, make in (('unreadable', self.unreadable), ('git-alias', self.alias)):
            with self.subTest(name):
                d = self.decide(make())
                self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                self.assertIn('rule=%s' % name, d.reason)

    def test_the_kept_rules_still_judge_the_line(self):
        # an unreadable line: the fallback reads the matching part again (git-checkout in main)
        d = self.decide(W.bash("echo 'x ; git checkout main", self.w.main))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-checkout'), d.reason)
        # a git alias: the call is judged as written (branch -D, with an alias beside it)
        d = self.decide(W.bash('git -c alias.x=status branch -D kit/old', self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-branch-force-delete'), d.reason)
        # a wrapped gate whose text names a kept rule's program is still judged by that rule
        d = self.decide(W.bash('python tools/gate.py --full | tail -5; git checkout main', self.w.main))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-checkout'), d.reason)

    def test_the_owner_can_turn_each_back_on(self):
        self.w.git.ancestors.add((W.SHA_WT, 'main'))
        self.w.user_config({'guard_rules': dict.fromkeys(
            ('wrapped-gate', 'busy-wait', 'unreadable', 'git-alias'), 'deny')})
        for name, make in (('wrapped-gate', self.wrapped_gate), ('busy-wait', self.busy_wait),
                           ('unreadable', self.unreadable), ('git-alias', self.alias)):
            with self.subTest(name):
                d = self.decide(make())
                self.assertEqual((d.verdict, d.rule), ('deny', name), d.reason)


class InlineAliasJudged(unittest.TestCase):
    """git-alias only warns, so a -c alias.<name>=<value> the line defines is judged as the
    command git runs: the value's words are the subcommand, as an alias from config is. Each row
    is denied with the kit's default modes and no machine config, as the command it hides is."""

    ROWS = (
        ("git -c alias.p='push --force origin main' p", 'git push --force origin main', 'git-push-main'),
        ("git -c alias.x='branch -D' x kit/y", 'git branch -D kit/y', 'git-branch-force-delete'),
        ("git -c alias.h='config core.hooksPath x' h", 'git config core.hooksPath x', 'git-hooks-path'),
        ('git -c alias.c=\'commit --author="A U Thor <a@example.invalid>"\' c -m x',
         'git commit --author="A U Thor <a@example.invalid>" -m x', 'owner-identity'),
    )

    def setUp(self):
        self.w = W.World(kit={'repo': {'name': 'demo'}, 'owner': {'email': 'a@example.invalid'}})
        self.addCleanup(self.w.cleanup)

    def decide(self, text):
        return guard.decide(action(W.bash(text, self.w.wt, sub=True)), self.w.ctx())

    def test_the_command_an_inline_alias_runs_is_judged(self):
        self.assertFalse(os.path.exists(os.path.join(os.path.join(self.w.home, user.DIR_NAME), 'config.json')))
        for text, plain, rule in self.ROWS:
            with self.subTest(text):
                d = self.decide(plain)
                self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
                d = self.decide(text)
                self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def test_an_alias_of_a_builtin_name_is_judged_as_written(self):
        # git ignores an alias that would hide a builtin: this runs `git status`
        d = self.decide("git -c alias.status='push --force origin main' status")
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
        self.assertIn('rule=git-alias', d.reason)
        d = self.decide("git -c 'alias.status=!git push --force origin main' status")
        self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)

    SHELL_ROWS = (
        "git -c 'alias.x=!git push --force origin main' x",
        "git -c 'alias.x=!git branch -D main' x",
        "git -c 'alias.y=!git branch -D main' -c alias.x=y x",
    )

    def test_a_shell_alias_the_line_defines_only_warns(self):
        # git-alias-shell was cut (honest mistakes only): a ! alias is written on purpose, no honest mistake;
        # git-alias warns and names the command it runs
        for text in self.SHELL_ROWS:
            with self.subTest(text):
                d = self.decide(text)
                self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                self.assertIn('rule=git-alias', d.reason)

    def test_an_archive_output_an_alias_gives_is_judged(self):
        # the --output word has no span in the line: no crash, the verdict of the plain command
        main = W.fwd(os.path.join(self.w.main, 'x.zip'))
        self.assertEqual(self.decide('git archive --output=%s HEAD' % main).verdict, 'deny')
        for out in ('x.zip', main):
            with self.subTest(out):
                plain = self.decide('git archive --output=%s HEAD' % out)
                d = self.decide("git -c alias.a='archive --output=%s' a HEAD" % out)
                self.assertEqual(d.verdict, plain.verdict, d.reason)
                if plain.verdict == 'deny':
                    self.assertEqual(d.rule, plain.rule, d.reason)

    def test_a_hint_writes_the_command_the_alias_runs(self):
        # git -C <main checkout> from the worktree: the same command in the worktree, built from
        # the expanded words, never the whole line again
        main = W.fwd(self.w.main)
        for text, cmd in (("git -C %s -c alias.x='rm --cached \"a b\"' x -q" % main, "rm --cached 'a b' -q"),
                          ("git -C %s -c alias.x='rm -q y' x" % main, 'rm -q y')):
            with self.subTest(text):
                d = self.decide(text)
                self.assertEqual(d.verdict, 'deny', d.reason)
                self.assertNotIn('alias.', d.instead)
                self.assertEqual(d.instead, 'git -C "%s" %s' % (W.fwd(self.w.wt), cmd))
        # a commit the alias makes: in the worktree plain git is refused too (git-plain-commit), so
        # the hint is the kit's commit tool there
        for text in ("git -C %s -c alias.x='commit -m \"a b\"' x --allow-empty" % main,
                     "git -C %s -c alias.x='commit -m y' x" % main):
            with self.subTest(text):
                d = self.decide(text)
                self.assertEqual(d.verdict, 'deny', d.reason)
                self.assertNotIn('alias.', d.instead)
                self.assertEqual(d.instead, 'python %s -C "%s" -m "<subject>" --model "<your model>" <each path you changed>'
                                 % (rules.kit_tool(self.w.ctx(), 'commit'), W.fwd(self.w.wt)))
        # a git the text cannot place (--git-dir): drop it, the same command in the worktree; a
        # commit there is the kit's commit tool
        d = self.decide("git --git-dir=../x -c alias.x='rm -q y' x")
        self.assertEqual(d.verdict, 'deny', d.reason)
        self.assertEqual(d.instead, 'git -C "%s" rm -q y' % W.fwd(self.w.wt))
        d = self.decide("git --git-dir=../x -c alias.x='commit -m y' x")
        self.assertEqual(d.verdict, 'deny', d.reason)
        self.assertEqual(d.instead, 'python %s -C "%s" -m "<subject>" --model "<your model>" <each path you changed>'
                         % (rules.kit_tool(self.w.ctx(), 'commit'), W.fwd(self.w.wt)))

    def test_a_hooks_path_setting_an_alias_gives_is_denied(self):
        # the alias is expanded, so git-hooks-path judges the clone it runs (the hint written out
        # from the alias's words was cut: the guard catches honest mistakes, not evasion)
        for text in ("git -c alias.cl='clone -c core.hooksPath=x' cl https://example.invalid/r.git",
                     "git -c alias.cl='clone -c' cl core.hooksPath=x https://example.invalid/r.git",
                     "git -c alias.cl='clone --config=core.hooksPath=x' cl https://example.invalid/r.git"):
            with self.subTest(text):
                d = self.decide(text)
                self.assertEqual((d.verdict, d.rule), ('deny', 'git-hooks-path'), d.reason)
        # the line's own words keep the line's own hint
        d = self.decide('git clone -c core.hooksPath=x https://example.invalid/r.git')
        self.assertEqual(d.instead, 'git clone https://example.invalid/r.git')


class HubKept(World):
    """Item 5 keeps the owner-only hub tools: with the kit's default modes, a parsed run of the hub
    tool's owner verb is denied to any session but the owner's, and so is a line the parser cannot
    read whose text names the hub tool, read verbs included (docs/guard.md "Fallback")."""

    def setUp(self):
        super().setUp()
        self.w.user_config({'pm_data': {'path': self.w.data}})
        self.hub = W.fwd(os.path.join(self.w.kit_dir, 'tools', 'hub.py'))

    def denied(self, line, rule):
        d = self.decide(W.bash(line, self.w.wt, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def test_a_parsed_owner_verb_is_denied_by_default(self):
        for line in ('python "%s" answer Q-1 --choice 1' % self.hub, 'python -m tools.hub todos done T-1',
                     'python "%s" todos add --owner "x"' % self.hub):
            with self.subTest(line):
                self.denied(line, 'hub-owner-verb')

    def test_the_owner_runs_it(self):
        line = 'python "%s" answer Q-1 --choice 1' % self.hub
        self.assertEqual(self.decide(W.bash(line, self.w.wt, sub=False)), Decision('allow'))

    def test_an_agent_verb_runs(self):
        self.assertEqual(self.decide(W.bash('python "%s" questions' % self.hub, self.w.wt, sub=True)),
                         Decision('allow'))

    def test_a_verb_on_a_line_the_parser_cannot_read_is_denied(self):
        for verb in ('questions', 'answer Q-1 --choice 1'):
            with self.subTest(verb):
                self.denied("echo 'x ; python \"%s\" %s" % (self.hub, verb), 'hub-owner-verb')

    def test_the_owner_runs_an_owner_verb_on_such_a_line(self):
        d = self.decide(W.bash("echo 'x ; python \"%s\" answer Q-1 --choice 1" % self.hub, self.w.wt))
        self.assertEqual(d.verdict, 'allow', d.reason)


class Warned(World):
    """Item 3: heavy (lock etiquette) warns; the owner's machine config makes it deny again
    (heredoc-winpath is cut: docs/guard-v2.md B8); a warned heavy line the fallback reads is still
    judged by the kept rules."""

    def heavy(self):
        return W.bash('godot --headless --path .', self.w.wt, sub=True)

    def test_the_kit_defaults(self):
        self.assertEqual(modes.DEFAULTS.get('heavy'), modes.WARN)
        self.assertNotIn('heredoc-winpath', modes.DEFAULTS)

    def test_each_warns_by_default(self):
        for name, make in (('heavy', self.heavy),):
            with self.subTest(name):
                d = self.decide(make())
                self.assertEqual((d.verdict, d.rule), ('allow', rules.WARNED), d.reason)
                self.assertIn('rule=%s' % name, d.reason)

    def test_the_owner_can_make_each_deny(self):
        self.w.user_config({'guard_rules': {'heavy': 'deny', 'heredoc-winpath': 'deny'}})
        for name, make in (('heavy', self.heavy),):
            with self.subTest(name):
                d = self.decide(make())
                self.assertEqual((d.verdict, d.rule), ('deny', name), d.reason)

    def test_a_heavy_program_behind_a_variable_is_still_judged_by_the_kept_rules(self):
        # the fallback's heavy check warns and goes on: the git checkout in main is still refused
        d = self.decide(W.bash('echo godot; "$X" --headless; git checkout main', self.w.main))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-checkout'), d.reason)
        # and a hidden run in a repository with a PM data record, a check after the heavy one
        common = os.path.join(self.w.main, '.git')
        os.makedirs(common, exist_ok=True)
        pmdata.write(common, 'pmdata', 'private', 'a' * 40)
        d = self.decide(W.bash('echo godot; "$X" --headless; eval "$Y"', self.w.main, sub=True))
        self.assertEqual((d.verdict, d.rule), ('deny', 'git-push-pmdata'), d.reason)

    def test_a_held_lock_says_nothing(self):
        self.w.held = True
        self.assertEqual(self.decide(self.heavy()), Decision('allow'))


class Simplified(World):
    """Item 4: hub-secrets reads only the token folder and token files spelled literally;
    git worktree remove is let through under a temp folder; a computed push refspec warns unless
    a flag or a literal `+` or `:` forces or deletes."""

    def setUp(self):
        super().setUp()
        self.w.user_config({'pm_data': {'path': self.w.data}})
        self.secrets = os.path.join(self.w.home, '.pm-kit', 'hub')
        self.store = os.path.join(self.w.data, 'hub')

    def passes(self, payload):
        d = self.decide(payload)
        self.assertEqual(d.verdict, 'allow', d.reason)
        return d

    def denied(self, payload, rule):
        d = self.decide(payload)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)

    def test_the_token_folder_spelled_literally_is_denied(self):
        sec = W.fwd(self.secrets)
        for line in ('cat %s/owner.token' % sec, 'ls %s' % sec, 'cat ~/.pm-kit/hub/agent.token',
                     'cat %s/x' % sec):
            with self.subTest(line):
                self.denied(W.bash(line, self.w.wt, sub=True), 'hub-secrets')

    def test_wildcards_the_word_hub_and_the_store_read_pass(self):
        home, st = W.fwd(self.w.home), W.fwd(self.store)
        for line, cwd in (('cat %s/.pm-kit/h?b/*' % home, self.w.wt),
                          ('ls %s/.pm-kit/h*' % home, self.w.wt),
                          ('grep -n hub launcher.json', self.w.data),
                          ('cat hub/todos/T-1.json', self.w.data),
                          ('cat "%s/questions/Q-1.json"' % st, self.w.wt)):
            with self.subTest(line):
                self.passes(W.bash(line, cwd, sub=True))

    def test_a_write_into_the_store_is_still_denied(self):
        st = W.fwd(self.store)
        self.denied(W.bash('echo x > "%s/todos/T-1.json"' % st, self.w.wt, sub=True), 'hub-secrets')
        self.denied(W.bash('rm -rf hub', self.w.data, sub=True), 'hub-secrets')

    def test_a_worktree_under_a_temp_folder_is_removed_dirty_or_unmerged(self):
        scratch = os.path.join(self.w.temp, 'scratch-wt')
        os.makedirs(scratch)
        self.assertEqual(self.decide(W.bash('git worktree remove %s' % W.fwd(scratch), self.w.wt, sub=True)),
                         Decision('allow'))
        self.assertEqual(self.decide(W.bash('git worktree remove --force %s' % W.fwd(scratch), self.w.main)),
                         Decision('allow'))

    def test_a_dirty_worktree_elsewhere_is_still_denied(self):
        self.denied(W.bash('git worktree remove %s' % W.fwd(self.w.wt), self.w.main, sub=True),
                    'git-worktree-remove')

    def test_a_computed_worktree_warns(self):
        d = self.passes(W.bash('git worktree remove "$WT"', self.w.wt, sub=True))
        self.assertEqual(d.rule, rules.WARNED)
        self.assertIn('rule=unplaced', d.reason)

    def test_a_computed_refspec_warns(self):
        for line in ('git push origin $BRANCH', 'git push origin "HEAD:$B"'):
            with self.subTest(line):
                d = self.passes(W.bash(line, self.w.wt, sub=True))
                self.assertEqual(d.rule, rules.WARNED)
                self.assertIn('rule=unplaced', d.reason)

    def test_a_computed_refspec_that_forces_or_deletes_is_denied(self):
        for line in ('git push --force origin $BRANCH', 'git push -f origin $B', 'git push origin "+$B"',
                     'git push origin ":$B"', 'git push --delete origin $B', 'git push --no-verify origin $B'):
            with self.subTest(line):
                self.denied(W.bash(line, self.w.wt, sub=True), 'git-push-main')

    def test_a_literal_force_or_delete_of_main_is_denied(self):
        for line in ('git push --force origin main', 'git push origin +kit/wt:main', 'git push origin :main',
                     'git push --delete origin main'):
            with self.subTest(line):
                self.denied(W.bash(line, self.w.wt, sub=True), 'git-push-main')


class RedirectHint(World):
    """Item 8: a redirect (or Out-File, Tee-Object, tee) refused in a main checkout names the same
    command aimed at the temp folder, not `git worktree add`; any other edit keeps the worktree
    alternative, and a session with its own worktree is still pointed there."""

    def denied(self, payload, rule):
        d = self.decide(payload)
        self.assertEqual((d.verdict, d.rule), ('deny', rule), d.reason)
        return d.reason

    def test_a_literal_redirect_in_main_names_the_temp_folder(self):
        tmp = W.fwd(self.w.temp)
        for shell, line, instead in (
                (W.bash, 'git log > out.txt', 'git log > "%s/out.txt"' % tmp),
                (W.bash, 'echo x > out.txt', 'echo x > "%s/out.txt"' % tmp),
                (W.bash, 'python -c "print(1)" > out.txt', 'python -c "print(1)" > "%s/out.txt"' % tmp),
                (W.ps, 'Get-ChildItem | Out-File out.txt', 'Out-File "%s/out.txt"' % tmp)):
            with self.subTest(line):
                reason = self.denied(shell(line, self.w.main, sub=True), 'main-checkout')
                self.assertIn('Instead: ' + instead, reason)
                self.assertIn('temp folder', reason)
                self.assertNotIn('worktree add', reason)

    def test_a_computed_redirect_in_main_names_the_temp_folder(self):
        W.deny_softened()
        self.addCleanup(W.allow_unplaced)
        reason = self.denied(W.bash('echo x > "$OUT"', self.w.main, sub=True), 'unplaced')
        self.assertIn('Instead: echo x > "%s/<file>"' % W.fwd(self.w.temp), reason)
        self.assertNotIn('worktree add', reason)

    def test_other_edits_in_main_keep_the_worktree_alternative(self):
        for payload in (write_ev(os.path.join(self.w.main, 'n.md'), self.w.main),
                        W.bash('cp a.txt out.txt', self.w.main, sub=True)):
            with self.subTest(payload):
                reason = self.denied(payload, 'main-checkout')
                self.assertIn('worktree add', reason)
                self.assertNotIn('temp folder', reason)

    def test_a_redirect_from_its_own_worktree_is_aimed_there(self):
        reason = self.denied(W.bash('echo x > %s/o.txt' % W.fwd(self.w.main), self.w.wt, sub=True), 'main-checkout')
        self.assertIn('Instead: echo x > "%s/o.txt"' % W.fwd(self.w.wt), reason)


class NetArgumentIndex(World):
    """_net_written's per-method argument index: Copy writes its destination, Move both paths,
    Replace all three; a literal main-checkout path in a written place is denied from a worktree."""

    def ps(self, text):
        return self.decide(W.ps(text.format(main=self.w.main, wt=self.w.wt), self.w.wt, sub=True))

    def denied(self, text):
        d = self.ps(text)
        self.assertEqual((d.verdict, d.rule), ('deny', 'main-checkout'), text)

    def test_copy_writes_its_destination_only(self):
        self.assertEqual(self.ps(r"[IO.File]::Copy('{main}\a.txt', '{wt}\b.txt')"), Decision('allow'))
        self.denied(r"[IO.File]::Copy('{wt}\a.txt', '{main}\b.txt')")

    def test_move_writes_both(self):
        self.denied(r"[IO.File]::Move('{main}\a.txt', '{wt}\b.txt')")
        self.denied(r"[IO.File]::Move('{wt}\a.txt', '{main}\b.txt')")

    def test_replace_writes_all_three(self):
        self.assertEqual(self.ps(r"[IO.File]::Replace('{wt}\a.txt', '{wt}\b.txt', '{wt}\c.bak')"), Decision('allow'))
        self.denied(r"[IO.File]::Replace('{main}\a.txt', '{wt}\b.txt', '{wt}\c.bak')")
        self.denied(r"[IO.File]::Replace('{wt}\a.txt', '{main}\b.txt', '{wt}\c.bak')")
        self.denied(r"[IO.File]::Replace('{wt}\a.txt', '{wt}\b.txt', '{main}\c.bak')")


class MachineConfigKey(unittest.TestCase):
    def setUp(self):
        from tests import home as thome
        self.home = thome.temp_home(self)
        self.write = lambda data: thome.machine_config(self.home, data)

    def test_guard_rules_takes_deny_warn_or_off(self):
        self.write({'guard_rules': {'heavy': 'off', 'main-checkout': 'warn', 'ledger': 'deny'}})
        self.assertEqual(user.user_config(self.home)['guard_rules'],
                         {'heavy': 'off', 'main-checkout': 'warn', 'ledger': 'deny'})
        for bad in ({'heavy': 'maybe'}, {'heavy': True}, ['heavy'], 'off'):
            self.write({'guard_rules': bad})
            with self.assertRaises(config.ConfigError, msg=repr(bad)) as cm:
                user.user_config(self.home)
            self.assertIn('guard_rules', str(cm.exception))


class Render(World):
    """How a warned allow reaches the agent on each host."""

    def setUp(self):
        World.setUp(self)
        self.w.user_config({'guard_rules': {'main-checkout': 'warn', 'write-jail': 'off'}})
        self.ev = write_ev(os.path.join(self.w.main, 'README.md'), self.w.wt)

    def test_claude_gives_the_warning_as_context_and_no_permission_decision(self):
        text, code = output(self.ev, self.w.ctx())
        self.assertEqual(code, 0)
        out = json.loads(text)['hookSpecificOutput']
        self.assertEqual(set(out), {'hookEventName', 'additionalContext'})
        self.assertEqual(out['hookEventName'], 'PreToolUse')
        self.assertIn('rule=main-checkout', out['additionalContext'])

    def test_the_switch_renders_a_warning_as_nothing(self):
        with mock.patch.object(claude_host, 'WARN_CONTEXT', False):
            self.assertEqual(output(self.ev, self.w.ctx()), ('', 0))

    def test_a_plain_allow_renders_as_nothing(self):
        self.assertEqual(claude_host.ADAPTER.render('PreToolUse', Decision('allow')), ('', 0))

    def test_codex_drops_the_warning(self):
        self.assertEqual(CODEX.render('PreToolUse', Decision('allow', 'warn', 'w')), ('', 0))


class HookRunner(unittest.TestCase):
    """kitlib.host.hookmain._judge keeps a warning: the allow it returns carries every action's."""

    def judge(self, decisions):
        it = iter(decisions)

        class Guard(object):
            @staticmethod
            def decide(a, environ=None, profile=None):
                return next(it)

        class Adapter(object):
            profile = None
        acts = tuple(Action('edit', 'Write', path='p%d' % i) for i in range(len(decisions)))
        return hookmain._judge(Adapter(), Guard(), acts, {}, None)

    def test_warnings_of_every_action_are_joined(self):
        d = self.judge([Decision('allow'), Decision('allow', 'warn', 'a'), Decision('allow', 'warn', 'b'),
                        Decision('allow', 'warn', 'a')])
        self.assertEqual((d.verdict, d.reason), ('allow', 'a b'))

    def test_an_ask_or_a_deny_still_decides(self):
        self.assertEqual(self.judge([Decision('allow', 'warn', 'a'), Decision('ask', 'r', 'q')]).verdict, 'ask')
        self.assertEqual(self.judge([Decision('allow', 'warn', 'a'), Decision('deny', 'r', 'q')]).verdict, 'deny')

    def test_a_plain_allow_stays_plain(self):
        self.assertEqual(self.judge([Decision('allow'), Decision('allow')]), Decision('allow'))


if __name__ == '__main__':
    unittest.main()
