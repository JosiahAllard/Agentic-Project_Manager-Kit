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
            'developer_instructions': 'Read docs/agent-profile.md and AGENTS.md in .codex/worktrees; "quote" \\ back.\n'
                                      'Trailer <noreply@openai.com>. Run as `python <kit>/hosts/codex/shell/scripts/'
                                      'launch.py --role kit-beta` on the account\'s model; ask by send_message, edit '
                                      'with apply_patch, in .codex/ and ~/.codex.\n'})
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
                self.assertEqual(got['sandbox_mode'], role.sandbox)
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
        self.assertEqual(cc.prose_models()['deep'], "the account's model")

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


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
