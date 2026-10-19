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
