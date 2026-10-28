"""Claude Code passes the host conformance suite (tests/host/conformance.py) on its recorded
payloads (tests/host/claude/payloads.json)."""
import json
import os
import unittest
from unittest import mock

from tests.host import bridge_conformance, conformance
from tests.host.test_leaks import ALLOWED, CONTENT_ALLOWED

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}/'


class ClaudeConformance(conformance.HostConformance, bridge_conformance.mixin('claude'),
                        unittest.TestCase):
    HOST = 'claude'
    FIXTURES = os.path.join(HERE, 'claude', 'payloads.json')
    # Upper bounds: test_leaks counts every host's name file by file, exactly; this counts Claude's.
    # tools/: the pm_watch seam, the launcher's account folders and the plugin refresh, each until
    # its step-6 wave moves it behind the adapter.
    LEAK_ALLOWED = dict(ALLOWED, **CONTENT_ALLOWED, **{
        'tools/pm_watch.py': 7,          # 7: claude_host.rc_records, the REMOTE check's reader
        'tools/launch.py': 1,
        'tools/plugin_refresh.py': 3,
    })

    def read_output(self, stdout, code):
        self.assertEqual(code, 0, 'Claude reads the verdict from the JSON, never the exit code')
        if not stdout:
            return 'allow', None
        out = json.loads(stdout)['hookSpecificOutput']
        return out['permissionDecision'], out['permissionDecisionReason']

    def read_context(self, stdout):
        return json.loads(stdout)['hookSpecificOutput']['additionalContext']

    def hook_scripts(self, manifests):
        """hooks.json's hooks, plus the guard, which machine setup installs into each account's
        settings (boot/hook.py claude --settings-entry) rather than the plugin."""
        from hosts.claude.adapter import content
        out = []
        for event, groups in json.loads(manifests[content.HOOKS_REL])['hooks'].items():
            for group in groups:
                for hook in group['hooks']:
                    script = hook['args'][0]
                    self.assertTrue(script.startswith(PLUGIN_ROOT), script)
                    out.append((event, script[len(PLUGIN_ROOT):], hook['args'][1:]))
        entry = self.adapter.settings_entry(conformance.ENTRY, executable='python')
        for event, groups in entry.items():
            out.extend((event, hook['args'][0], hook['args'][1:]) for g in groups for hook in g['hooks'])
        return out

    def test_the_start_parts_run_through_the_one_entry(self):
        """The coordinator rules (hooks.json) and the PM checklist (the PM settings, docs/pm-start.md)
        are named parts of SessionStart; a part SubagentStart or the guard lacks fails."""
        from hosts.claude.adapter import host
        self.assertEqual(host.PARTS, ('coordinator', 'pm'))
        code, out = self.run_hook('SessionStart', {'cwd': self.KIT_ROOT}, None, part='coordinator')
        self.assertEqual((code, out), (0, ''), 'an unmarked session gets no coordinator rules')
        with mock.patch('kitlib.pmstart.load_config', return_value={}), \
                mock.patch('kitlib.pmstart.checklist', return_value='the PM checklist'):
            code, out = self.run_hook('SessionStart', {}, None, part='pm')
        self.assertEqual(self.read_context(out), 'the PM checklist')
        for event, part in (('SubagentStart', 'pm'), ('SessionStart', 'nope'), ('PreToolUse', 'pm')):
            with self.assertRaises(ValueError):
                self.run_hook(event, {}, None, part=part)

    # session_host(): None. ADAPTER.sessions() is hosts.claude.adapter.claude_host, the transcript
    # helpers kitlib.pmwatch is handed; it has no sessions/loaded/usage/models yet.

    def bridge_delivery(self):
        """The bridge half's peer adapter on the owned fake recipient pipe of tests/bridge/
        test_claude_peer.py: a registry record and key for this process, and its private listener."""
        from tests.bridge.test_claude_peer import ClaudePeerTests
        fixture = ClaudePeerTests('test_native_auth_and_exact_operation_reach_original_chat_but_write_is_unconfirmed')
        fixture.setUp()
        self.addCleanup(fixture.tearDown)

        def receive():
            thread = fixture.listen()

            def done():
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(fixture.errors, [])
                return list(fixture.received)
            return done
        return fixture.adapter, fixture.message, receive


if __name__ == '__main__':
    unittest.main()
