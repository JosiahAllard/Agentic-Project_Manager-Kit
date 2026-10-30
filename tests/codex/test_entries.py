"""The Codex plugin's entries run as Codex runs them, from the plugin root: the export folder Codex
installs into its cache, which is laid out as the kit is, so these run them from the kit root
(boot/hook.py, hosts/codex/shell/mcp/). Each runs in a child Python with closed stdin."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kitlib import sessionmark
from kitlib.host import header

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT                           # the plugin root: the export, laid out as the kit
HOOK = 'boot/hook.py'                   # the kit's one hook entry, hooks.json's ${PLUGIN_ROOT}/boot/hook.py
MCP = 'hosts/codex/shell/mcp/'          # mcp.json's servers, from the plugin root


def _session(line):
    """A real-log line's session, None for a line that does not parse (readers skip those)."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row.get('session') if isinstance(row, dict) else None


class Entries(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix='codex-cache-')
        self.addCleanup(scratch.cleanup)
        self.base = Path(scratch.name)
        self.home = self.base / 'home'
        self.home.mkdir()
        # The home fence marker stays in the child's environment: without it the child is unfenced
        # and reads the machine's real PM-repo pointer, so a deny appends to the real refusal log.
        # The old reason for dropping it still holds (kitlib/config/user.py _refused, the
        # UNPROVEN_MESSAGE raise): where the system will not name the profile folder the fence
        # refuses even a temp home. That only makes a child's home lookup raise HomeFenced, which
        # refusal_log.record swallows (no line); the pointer is also left unnamed below, so both
        # cases write nothing. No CI workflow in the repo runs this file.
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith('MACHINE_LOCK_')
                    and k not in ('PM_KIT_ROLE', 'PM_KIT_RELEASE_ID', 'KIT_PM_REPO_FENCE')}
        self.env.update(PM_KIT_ROOT=str(ROOT), HOME=str(self.home), USERPROFILE=str(self.home),
                        GIT_CEILING_DIRECTORIES=str(self.base), PYTHONDONTWRITEBYTECODE='1')

    def run_entry(self, plugin, relative, text='', args=()):
        return subprocess.run([sys.executable, str(plugin / relative), *args], input=text, capture_output=True,
                              text=True, encoding='utf-8', env=self.env, cwd=str(self.base), timeout=60)

    def tool(self, name, data):
        return json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': name, 'tool_input': data,
                           'cwd': str(self.base)})

    def test_a_deny_is_codex_permission_json(self):
        r = self.run_entry(PLUGIN, HOOK, self.tool('Bash', {'command': 'find $USERPROFILE -name x',
                                                            'shell': 'bash.exe'}), ('codex', 'PreToolUse'))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)['hookSpecificOutput']
        self.assertEqual((out['hookEventName'], out['permissionDecision']), ('PreToolUse', 'deny'))
        self.assertIn('rule=root-search', out['permissionDecisionReason'])

    def test_a_deny_never_reaches_the_real_refusal_log(self):
        from kitlib.config import pointer
        from kitlib.guard import refusal_log
        value, _ = pointer.registry()             # the machine's real PM repository, read before any fence
        if not value:
            self.skipTest('no PM repository pointer on this machine: no real log to protect')
        real = refusal_log.log_path(value)

        probe = 'fence-probe-%d' % os.getpid()     # names this test's children in any line they write
        self.env[sessionmark.SESSION_NAME_ENV] = probe

        def mine():
            if not os.path.exists(real):
                return 0
            with open(real, encoding='utf-8') as fh:
                return sum(1 for line in fh if _session(line) == probe)
        before = mine()
        denied = self.tool('Bash', {'command': 'find $USERPROFILE -name x', 'shell': 'bash.exe'})
        for text in (denied, '{bad'):
            r = self.run_entry(PLUGIN, HOOK, text, ('codex', 'PreToolUse'))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn('deny', r.stdout)
        self.assertEqual(mine(), before, 'a test child appended to the real refusal log: ' + real)

    def test_an_allow_writes_nothing(self):
        r = self.run_entry(PLUGIN, HOOK, self.tool('Read', {'file_path': 'a'}), ('codex', 'PreToolUse'))
        self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)

    def test_a_bad_payload_is_a_deny(self):
        for text in ('{bad', '[]', self.tool('Write', {})):
            r = self.run_entry(PLUGIN, HOOK, text, ('codex', 'PreToolUse'))
            self.assertEqual(r.returncode, 0, r.stderr)
            out = json.loads(r.stdout)['hookSpecificOutput']
            self.assertEqual(out['permissionDecision'], 'deny')
            self.assertIn('rule=guard-error', out['permissionDecisionReason'])

    def test_a_kit_that_will_not_boot_blocks_by_exit_code(self):
        self.env['PM_KIT_ROOT'] = str(self.base / 'missing')
        r = self.run_entry(PLUGIN, HOOK, self.tool('Write', {'file_path': 'a'}), ('codex', 'PreToolUse'))
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        fast = self.run_entry(PLUGIN, HOOK, self.tool('Read', {}), ('codex', 'PreToolUse'))
        self.assertEqual((fast.returncode, fast.stdout), (0, ''), 'the owner\'s read never boots the kit')
        self.env['PM_KIT_ROLE'] = 'worker'
        marked = self.run_entry(PLUGIN, HOOK, self.tool('Read', {}), ('codex', 'PreToolUse'))
        self.assertEqual(marked.returncode, 2, 'a marked session boots the kit for every call')
        s = self.run_entry(PLUGIN, HOOK, '{}', ('codex', 'SessionStart'))
        self.assertEqual(s.returncode, 0, s.stderr)
        self.assertIn('the kit did not load', s.stdout)

    def test_a_start_event_adds_the_kit_header(self):
        for event in ('SessionStart', 'PostCompact', 'SubagentStart'):
            with self.subTest(event=event):
                r = self.run_entry(PLUGIN, HOOK, json.dumps({'cwd': str(self.base)}), ('codex', event))
                self.assertEqual(r.returncode, 0, r.stderr)
                out = json.loads(r.stdout)['hookSpecificOutput']
                self.assertEqual(out['hookEventName'], event)
                self.assertTrue(out['additionalContext'].startswith(header.text(str(ROOT))))

    def test_the_lock_server_lists_reads_and_refuses_writes(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                     'params': {'name': 'lock_hold_create', 'arguments': {'spec': {}}}}]
        r = self.run_entry(PLUGIN, MCP + 'lock_server.py', '\n'.join(json.dumps(q) for q in requests) + '\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second, third = [json.loads(line) for line in r.stdout.splitlines()]
        from hosts.codex.adapter.lock import READ_TOOLS
        self.assertEqual({t['name'] for t in second['result']['tools']}, READ_TOOLS)
        # A retired owner write is no tool of the server's at all: an unknown tool.
        self.assertNotIn('result', third)
        self.assertEqual(third['error']['code'], -32602)

    def test_the_hub_server_lists_its_tools(self):
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
        r = self.run_entry(PLUGIN, MCP + 'hub_server.py', '\n'.join(json.dumps(q) for q in requests) + '\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        first, second = [json.loads(line) for line in r.stdout.splitlines()]
        names = {t['name'] for t in second['result']['tools']}
        self.assertIn('hub_pending', names)
        from kitlib import hubmcp
        self.assertFalse(names & set(hubmcp.OWNER_ONLY))


if __name__ == '__main__':
    unittest.main()
