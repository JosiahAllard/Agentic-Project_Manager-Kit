"""The cheap guard (KIT-GUARD-CHEAP): rule modes (kitlib.guard.modes) and how a warned call reaches
the agent, the rules that now warn or stop, and the denies it keeps."""
import json
import os
import unittest
from unittest import mock

from hosts.claude.adapter import host as claude_host
from hosts.codex.adapter.host import ADAPTER as CODEX
from kitlib import config
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


class StrictGitReads(World):
    """Where the checkout cannot be placed (git -C $x), git_reads(strict) keeps each form below a
    non-read exactly as before the cheap guard: it is refused, under unplaced (held at deny here;
    its default warns) where it was write-jail, and a PowerShell splat stays write-jail. A plain
    read, and a computed word strict_dyn_read allows, is let through as before."""

    NON_READS = (
        'log $f', 'show "$f"', 'diff $a $b',                     # computed words strict_dyn_read refuses
        'log --output=f', 'show --outp=f', 'diff --output f',    # writes_output_file
        'grep -O foo', 'grep -nO foo', 'grep --open foo',        # opens_pager
        'archive --remote=x HEAD', 'archive --exec=x HEAD',      # ARCHIVE_RUNS
        'branch --list --unset-up', 'tag -l --del', 'config --get --repl a.b c')   # strict_read_opts
    READS = ('log', 'show HEAD', 'diff', 'grep -n foo', 'branch -vv', 'tag -l', 'config --get a.b',
             'config -f x.cfg --get a.b',
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

    def test_a_powershell_splat_stays_denied(self):
        d = self.git('log @f', W.ps)
        self.assertEqual((d.verdict, d.rule), ('deny', 'write-jail'))
        self.assertEqual(self.git("log '@f'", W.ps), Decision('allow'))    # quoted: plain text

    def test_a_plain_read_is_let_through(self):
        W.deny_unplaced()
        self.addCleanup(W.allow_unplaced)
        for rest in self.READS:
            self.assertEqual(self.git(rest), Decision('allow'), rest)
        self.assertEqual(self.git('log "HEAD~$n"', W.ps), Decision('allow'))


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
        import shutil
        import tempfile
        self.home = tempfile.mkdtemp(prefix='kit-home-')
        self.addCleanup(shutil.rmtree, self.home, True)

    def write(self, data):
        os.makedirs(os.path.join(self.home, user.DIR_NAME), exist_ok=True)
        with open(user.config_file(self.home), 'w', encoding='utf-8') as f:
            json.dump(data, f)

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
