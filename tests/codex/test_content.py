"""hosts.codex.adapter.content: every role rendered as a Codex agent file that TOML reads back, its
instructions the role body with Codex's token values, and the plugin's manifests (the Codex branch's
roles.json and committed TOMLs, replaced; docs/content.md "Codex")."""
import json
import os
import unittest

try:
    import tomllib
except ImportError:                     # Python < 3.11: the parse checks skip
    tomllib = None

from hosts.codex.adapter import content as cc
from kitlib import content as kcontent
from kitlib.host import content as hc
from kitlib.host import registry

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLE = (b'---\nname: alpha\ndescription: "Builds: things. Ask {{agent:beta}} first."\ntier: deep\n'
        b'sandbox: read-only\ncapabilities: [read]\nmode: read\n---\n'
        b'Read {{profile_path}} and {{instructions_file}} in {{worktrees_dir}}; "quote" \\ back.\n'
        b'Trailer <{{coauthor_email}}>. Run as `{{launch_agent}} {{agent:beta}}` on {{model:strong}}; '
        b'ask by {{tool:message}}, edit with {{tool:edit}}, in {{owner_folder}} and {{user_folder}}.\n')
BETA = (b'---\nname: beta\ndescription: b\ntier: light\neffort: high\nsandbox: workspace-write\n'
        b'capabilities: all\nmodel.codex: gpt-x\n---\r\nbody\r\n')


@unittest.skipIf(tomllib is None, 'tomllib needs Python 3.11')
class Agents(unittest.TestCase):
    def render(self, *roles):
        c = hc.Content(roles=tuple(hc.parse_role(r) for r in roles), skills=())
        return cc.renderer().render(c)

    def test_an_agent_file_reads_back(self):
        files = self.render(ROLE, BETA)
        alpha = tomllib.loads(files['hosts/codex/shell/agents/kit-alpha.toml'].decode('utf-8'))
        self.assertEqual(alpha, {
            'name': 'kit-alpha', 'description': 'Builds: things. Ask kit-beta first.',
            'model_reasoning_effort': 'xhigh', 'sandbox_mode': 'read-only',
            'developer_instructions': 'Read .codex/agent-profile.md and AGENTS.md in .codex/worktrees; "quote" \\ back.\n'
                                      'Trailer <noreply@openai.com>. Run as `python <kit>/hosts/codex/shell/scripts/'
                                      'launch.py --role kit-beta` on strong-tier; ask by send_message (not available '
                                      'to a Codex agent: ask the PM), edit with apply_patch, in .codex/ and ~/.codex.\n'})
        beta = files['hosts/codex/shell/agents/kit-beta.toml']
        self.assertIn(b'\r\n', beta, 'the role file\'s line endings')
        beta = tomllib.loads(beta.decode('utf-8'))
        self.assertEqual((beta['model'], beta['model_reasoning_effort'], beta['sandbox_mode'],
                          beta['developer_instructions']), ('gpt-x', 'high', 'workspace-write', 'body\n'))

    def test_every_kit_role_reads_back_with_its_body(self):
        c = kcontent.load(ROOT)
        files = cc.renderer().render(c)
        for role in c.roles:
            with self.subTest(role=role.name):
                got = tomllib.loads(files['hosts/codex/shell/agents/kit-%s.toml' % role.name].decode('utf-8'))
                self.assertEqual(got['name'], 'kit-' + role.name)
                self.assertEqual(got['sandbox_mode'], cc.sandbox_mode(role))
                self.assertEqual(got['sandbox_mode'] == 'read-only', role.mode in ('read', 'lander') or role.sandbox == 'read-only')
                self.assertEqual(got['developer_instructions'],
                                 cc.substitute_body(role, c.role_names()).replace('\r\n', '\n'))
                self.assertNotIn('model', got, 'every tier is the account default')
                self.assertNotIn('{{', got['developer_instructions'])

    def test_no_rendered_role_names_the_other_hosts_values(self):
        from hosts.claude.adapter import content as claude
        c = kcontent.load(ROOT)
        files = cc.renderer().render(c)
        values = [v for v in claude.tokens().values()] + ['pm-kit:', 'CLAUDE.md']
        for path, data in files.items():
            if path.endswith('.toml'):
                for value in values:
                    self.assertNotIn(value.encode(), data, (path, value))


class Tokens(unittest.TestCase):
    def test_every_token_has_a_codex_value(self):
        self.assertEqual(sorted(cc.tokens()), sorted(t for t in hc.TOKENS if t not in hc.ARG_TOKENS))
        self.assertEqual(sorted(cc.TOOLS), sorted(hc.CAPABILITIES))
        self.assertEqual(sorted(cc.prose_models()), sorted(hc.TIERS))

    def test_the_tiers_are_hosts_json(self):
        self.assertEqual(cc.renderer().tier_models(), dict(registry.host('codex').tiers))
        self.assertEqual(sorted(cc.TIER_EFFORT), sorted(hc.TIERS))
        self.assertEqual(cc.prose_models()['deep'], 'deep-tier')

    def test_the_profiles_live_in_codexs_owner_only_folder(self):
        """Both profiles sit in the folder hosts.json's Codex owner_paths lock, as Claude's sit in
        .claude/, so no repository's own owner.paths can open them to an agent."""
        values = cc.tokens()
        folder = values['owner_folder']
        self.assertIn(folder, registry.host('codex').profile.owner_paths)
        self.assertEqual(values['profile_path'], folder + 'agent-profile.md')
        self.assertEqual(values['coordinator_profile_path'], folder + 'coordinator-profile.md')

    def test_the_kits_own_codex_profile_points_to_the_real_one(self):
        """This repository's profile is docs/agent-profile.md; each host's own-folder stub (Claude's
        .claude/agent-profile.md, Codex's profile_path) points to it, heading for heading."""
        def headings(rel):
            with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
                return [line.strip() for line in f if line.startswith('#') and not line.startswith('# ')]
        stub = cc.tokens()['profile_path']
        self.assertEqual(headings(stub), headings('docs/agent-profile.md'))
        self.assertEqual(headings(stub), headings('.claude/agent-profile.md'))
        with open(os.path.join(ROOT, stub), encoding='utf-8') as f:
            self.assertIn('`docs/agent-profile.md`', f.read())

    def test_strings_escape(self):
        if tomllib is None:
            self.skipTest('tomllib needs Python 3.11')
        for value in ('plain', 'a "b" \\ c', 'tab\there', 'line\nbreak', 'ctl\x01', 'del\x7f', 'ünïcode'):
            with self.subTest(value=value):
                self.assertEqual(tomllib.loads('v = %s' % cc.toml_string(value))['v'], value)
                self.assertEqual(tomllib.loads('v = %s' % cc.toml_block(value, '\n'))['v'], value)


class Manifests(unittest.TestCase):
    def test_the_plugin_files(self):
        kit = kcontent.load_kit(ROOT)
        files = cc.renderer().manifests(kit, kcontent.load(ROOT))
        plugin = json.loads(files[cc.MANIFEST_REL])
        self.assertEqual((plugin['name'], plugin['version']), (kit['name'], kit['version']))
        self.assertEqual(plugin['extensions']['com.openai']['hooks'], './hooks/hooks.json')
        mcp = json.loads(files[cc.MCP_REL])['mcpServers']['machine-lock']
        self.assertEqual(mcp['args'], ['${PLUGIN_ROOT}/mcp/lock_server.py'])
        self.assertTrue(os.path.isfile(os.path.join(ROOT, *cc.SHELL_REL.split('/'), 'mcp', 'lock_server.py')))
        hooks = json.loads(files[cc.HOOKS_REL])['hooks']
        self.assertEqual(sorted(hooks), sorted(registry.host('codex').capabilities.hook_events))
        for event, groups in hooks.items():
            self.assertEqual(groups[0]['hooks'][0]['command'], 'pythonw "${PLUGIN_ROOT}/hooks/run.py" ' + event)

    def test_the_hook_events_must_match_hosts_json(self):
        r = cc.renderer()
        with unittest.mock.patch.object(cc, 'HOOKS', cc.HOOKS[:-1]):
            with self.assertRaises(hc.ContentError):
                r.hooks()


class Vendored(unittest.TestCase):
    def test_the_plugin_ships_the_kits_boot_check_byte_for_byte(self):
        files = cc.renderer().vendored(ROOT)
        with open(os.path.join(ROOT, 'boot', 'kitboot.py'), 'rb') as f:
            self.assertEqual(files, {'hosts/codex/shell/boot/kitboot.py': f.read()})
        self.assertIn('hosts/codex/shell/boot/kitboot.py', kcontent.render(ROOT, [cc.renderer()]))

    def test_the_entrys_fast_path_is_only_the_policys_read_and_bookkeeping_tools(self):
        import importlib.util
        from hosts.codex.adapter import policy
        spec = importlib.util.spec_from_file_location(
            'codex_run', os.path.join(ROOT, 'hosts', 'codex', 'shell', 'hooks', 'run.py'))
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        self.assertTrue(run.FAST <= policy.ALLOWED_TOOLS - {'write_stdin'})
        self.assertLess(run.DEADLINE, cc.HOOK_TIMEOUT, 'inside the timeout hooks.json gives Codex')

    def load_run(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'codex_run', os.path.join(ROOT, 'hosts', 'codex', 'shell', 'hooks', 'run.py'))
        run = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(run)
        return run

    def test_the_entrys_budget_counts_the_boot(self):
        """DEADLINE is the whole call's: the time the boot took (a pinned verify hashes every file)
        comes off what hookmain gets, which names the whole budget when it runs out."""
        import io
        import sys
        import types
        run = self.load_run()
        clock = [100.0]
        boot = types.ModuleType('kitboot')

        def slow_boot(here=None):
            clock[0] += 3.0
            return 'kit'
        boot.boot = slow_boot
        seen = {}

        def fake_run(event, kit, text, stdout, entry, deadline, started):
            seen.update(deadline=deadline, started=started)
            return 0
        with unittest.mock.patch.dict(sys.modules, {'kitboot': boot}), \
                unittest.mock.patch('time.monotonic', lambda: clock[0]), \
                unittest.mock.patch('hosts.codex.adapter.entry.run', fake_run):
            rc = run.main(['PreToolUse'], io.StringIO('{"tool_name": "Write"}'), io.StringIO(), {})
        self.assertEqual(rc, 0)
        self.assertEqual((seen['deadline'], seen['started']), (run.DEADLINE, 100.0))

    def test_the_entrys_fast_path_reads_the_mark_as_sessionmark_does(self):
        """run.py reads the mark before the kit boots, so it cannot import kitlib.sessionmark: its
        MARK and test are held to sessionmark.ENV and sessionmark.mark's None."""
        import io
        import sys
        import types
        from kitlib import sessionmark
        run = self.load_run()
        self.assertEqual(run.MARK, sessionmark.ENV)
        boot = types.ModuleType('kitboot')

        def no_boot(here=None):
            raise RuntimeError('booted')
        boot.boot = no_boot
        for value in ('', '   ', 'worker', ' reviewer ', 'owner', 'pm-kit:worker', 'pm-kit:'):
            with self.subTest(value=value), unittest.mock.patch.dict(sys.modules, {'kitboot': boot}):
                rc = run.main(['PreToolUse'], io.StringIO('{"tool_name": "Read"}'), io.StringIO(),
                              {sessionmark.ENV: value})
                self.assertEqual(rc, 0 if sessionmark.mark({sessionmark.ENV: value}) is None else 2)


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
