"""kitlib.content, kitlib.host.content and hosts.claude.adapter.content (docs/content.md).

The drift test runs on the real content/ and the committed hosts/claude/shell/agents and hosts/claude/shell/skills: they
must be exactly what the renderer writes. The rest run on temp copies and small role files.
"""
import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

from hosts.claude.adapter import content as claude_content
from kitlib import content
from kitlib.host import content as hc
from kitlib.host import registry

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(KIT_ROOT, 'tools'))
import render_content as cli  # noqa: E402

ROLE = (b'---\nname: alpha\ndescription: Builds things. Ask {{agent:beta}} first.\ntier: standard\n'
        b'effort: low\nsandbox: workspace-write\ncapabilities: [read, search, bash]\n---\n\n'
        b'Read {{profile_path}} and {{instructions_file}}; docs at <kit>/docs.\n')
BETA = b'---\nname: beta\ndescription: "Two: parts"\ntier: deep\nsandbox: read-only\ncapabilities: all\n---\nbody\n'


def read(path):
    with open(path, 'rb') as f:
        return f.read()


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(data)


class RealContentTest(unittest.TestCase):
    """The committed files are the renderer's: an edit to hosts/claude/shell/agents or hosts/claude/shell/skills by hand
    fails here; edit content/ and run `python tools/render_content.py --write`."""

    def test_no_drift(self):
        self.assertEqual(content.drift(KIT_ROOT, [claude_content.renderer()]), [])

    def test_cli_default_is_check(self):
        out = io.StringIO()
        self.assertEqual(cli.main([], out), 0, out.getvalue())

    def test_every_role_renders_to_an_agent_file_and_every_skill_to_its_folder(self):
        c = content.load(KIT_ROOT)
        agents = os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents')
        self.assertEqual(sorted(n[:-3] for n in os.listdir(agents) if n.endswith('.md')),
                         sorted(c.role_names()))
        skills = os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'skills')
        self.assertEqual(sorted(os.listdir(skills)), sorted(s.name for s in c.skills))

    def test_the_plugin_manifest_lists_every_role(self):
        with open(os.path.join(KIT_ROOT, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
            agents = json.load(f)['agents']
        self.assertEqual(sorted(agents), sorted('./hosts/claude/shell/agents/%s.md' % n for n in content.load(KIT_ROOT).role_names()))

    def test_skills_are_copied_byte_for_byte(self):
        for s in content.load(KIT_ROOT).skills:
            for rel, data in s.files:
                self.assertEqual(read(os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'skills', s.name, *rel.split('/'))), data)

    def test_no_agent_file_names_the_plugin_root_variable(self):
        """An agent body gets no ${CLAUDE_PLUGIN_ROOT} substitution: the kit's folder is `<kit>`."""
        agents = os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents')
        for n in os.listdir(agents):
            with self.subTest(agent=n):
                self.assertNotIn(b'CLAUDE_PLUGIN_ROOT', read(os.path.join(agents, n)))


class ManifestTest(unittest.TestCase):
    """The plugin manifest, the marketplace and hooks.json: rendered from kit.json (the one version)."""

    def load(self, *parts):
        with open(os.path.join(KIT_ROOT, *parts), encoding='utf-8') as f:
            return json.load(f)

    def test_every_manifest_carries_kit_json_version(self):
        kit = content.load_kit(KIT_ROOT)
        self.assertEqual(self.load('kit.json'), kit)
        plugin = self.load('.claude-plugin', 'plugin.json')
        self.assertEqual((plugin['name'], plugin['version']), (kit['name'], kit['version']))
        market = self.load('.claude-plugin', 'marketplace.json')
        self.assertEqual(market['metadata']['version'], kit['version'])
        self.assertEqual([p['source'] for p in market['plugins']], ['./'])

    def test_the_hook_commands_come_from_the_one_helper(self):
        from kitlib.host import hookmain, registry
        want = hookmain.manifest_command('x', registry.host('claude').platforms)['command']
        hooks = self.load('hosts', 'claude', 'shell', 'hooks', 'hooks.json')['hooks']
        self.assertEqual(sorted(hooks), ['SessionStart', 'SubagentStart'])
        for event, groups in hooks.items():
            for h in (h for g in groups for h in g['hooks']):
                self.assertEqual(h['command'], want)
                self.assertTrue(h['args'][0].startswith('${CLAUDE_PLUGIN_ROOT}/hosts/claude/shell/hooks/'))

    def test_load_kit_refusals(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        for raw in (None, b'{', b'[]', b'{"name": "pm-kit"}', b'{"name": "PM", "version": "1.0.0"}',
                    b'{"name": "pm-kit", "version": "1.0"}', b'{"name": "pm-kit", "version": "1.0.0", "x": 1}'):
            path = os.path.join(root, 'kit.json')
            if os.path.exists(path):
                os.remove(path)
            if raw is not None:
                write(path, raw)
            with self.subTest(raw=raw), self.assertRaises(hc.ContentError):
                content.load_kit(root)

    def test_every_role_names_its_policy_mode(self):
        """The owner's table (docs/guard.md "Role policy"): leads control, builders write, the
        read-only roles read, the test-runner tests, the lander lands. role_modes reads the same."""
        want = {'pm': 'controller', 'coordinator': 'controller',
                'worker': 'write', 'senior-worker': 'write', 'strong-worker': 'write', 'deep-worker': 'write',
                'asset-smith': 'write', 'sound-smith': 'write', 'tech-artist': 'write', 'spec-writer': 'write',
                'records': 'write', 'reviewer': 'read', 'researcher': 'read', 'consultant': 'read',
                'test-runner': 'test', 'lander': 'lander'}
        self.assertEqual({r.name: r.mode for r in content.load(KIT_ROOT).roles}, want)
        self.assertEqual(content.role_modes(KIT_ROOT), want)
        self.assertEqual(content.role_mode('opus-worker', KIT_ROOT), 'write')
        self.assertEqual({r.name: r.writes for r in content.load(KIT_ROOT).roles if r.writes},
                         {'consultant': ('docs/ops/consultant-notes.local.md',)})
        self.assertEqual(content.role_writes('consultant', KIT_ROOT), ('docs/ops/consultant-notes.local.md',))
        for r in content.load(KIT_ROOT).roles:
            with self.subTest(r.name):
                agent = read(os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents', r.name + '.md'))
                self.assertNotIn(b'\nmode:', agent)
                self.assertNotIn(b'\nwrites:', agent)

    def test_role_bodies_name_no_claude_value_a_token_covers(self):
        """The values in hosts.claude.adapter.content.tokens() reach the agent files only through tokens;
        `<kit>`, the neutral name of the kit's folder (kit_root's Claude value too), is written bare."""
        for r in content.load(KIT_ROOT).roles:
            text = r.description + r.body
            for value in [v for v in claude_content.tokens().values() if v != '<kit>'] + ['pm-kit:']:
                with self.subTest(role=r.name, value=value):
                    self.assertNotIn(value, text)
            # the tools a role names for messaging and dispatch, and the tiers' models: {{tool:...}},
            # {{model:...}}
            for value in ('SendMessage', 'ListAgents', 'Agent tool', 'EnterWorktree', 'Glob/Grep'):
                with self.subTest(role=r.name, value=value):
                    self.assertNotIn(value, text)
            for model in set(claude_content.renderer().tier_models().values()):
                with self.subTest(role=r.name, model=model):
                    self.assertIsNone(re.search(r'(?i)\b%s\b' % re.escape(model), text))


class ParseTest(unittest.TestCase):
    def test_a_role(self):
        r = hc.parse_role(ROLE)
        self.assertEqual((r.name, r.tier, r.effort, r.sandbox, r.capabilities, r.all_tools, r.cache),
                         ('alpha', 'standard', 'low', 'workspace-write', ('read', 'search', 'bash'), False, None))
        self.assertTrue(r.body.startswith('\nRead {{profile_path}}'))
        self.assertEqual(r.newline, '\n')

    def test_all_and_a_quoted_description_and_crlf(self):
        r = hc.parse_role(BETA.replace(b'\n', b'\r\n'))
        self.assertTrue(r.all_tools)
        self.assertEqual(r.capabilities, ())
        self.assertEqual(r.description, 'Two: parts')
        self.assertEqual(r.newline, '\r\n')
        self.assertEqual(r.body, 'body\r\n')

    def test_a_model_override_beats_the_tier(self):
        r = hc.parse_role(BETA.replace(b'tier: deep\n', b'tier: deep\nmodel.claude: sonnet\n'))
        self.assertEqual(r.models, (('claude', 'sonnet'),))
        self.assertEqual(r.model_for('claude', {'deep': 'opus'}), 'sonnet')
        self.assertEqual(r.model_for('other', {'deep': 'big'}), 'big')

    def test_refusals(self):
        for old, new in ((b'tier: standard', b'tier: huge'), (b'sandbox: workspace-write', b'sandbox: open'),
                         (b'effort: low', b'effort: some'), (b'[read, search, bash]', b'[read, telepathy]'),
                         (b'[read, search, bash]', b'[read, read]'), (b'[read, search, bash]', b'read'),
                         (b'name: alpha', b'name: Alpha'), (b'effort: low', b'colour: red'),
                         (b'tier: standard\n', b''), (b'effort: low', b'effort: low\neffort: high'),
                         (b'---\n\n', b'\n\n'), (b'description: Builds', b'description: "Builds')):
            with self.subTest(new=new):
                with self.assertRaises(hc.ContentError):
                    hc.parse_role(ROLE.replace(old, new, 1))

    def test_a_mode(self):
        self.assertIsNone(hc.parse_role(ROLE).mode)
        for m in hc.MODES:
            with self.subTest(m):
                self.assertEqual(hc.parse_role(ROLE.replace(b'effort: low\n', b'effort: low\nmode: %s\n' % m.encode())).mode, m)
        for bad in (b'mode: admin\n', b'mode: write\nmode: read\n', b'mode: \n'):
            with self.subTest(bad=bad), self.assertRaises(hc.ContentError):
                hc.parse_role(ROLE.replace(b'effort: low\n', b'effort: low\n' + bad))

    def test_writes(self):
        self.assertEqual(hc.parse_role(ROLE).writes, ())
        r = hc.parse_role(ROLE.replace(b'effort: low\n', b'effort: low\nwrites: [docs/a.md, notes/*.txt]\n'))
        self.assertEqual(r.writes, ('docs/a.md', 'notes/*.txt'))
        for bad in (b'[]', b'all', b'docs/a.md', b'[/etc/x]', b'[../x]', b'[docs/../x]', b'[C:/x]',
                    b'[docs\\\\x]', b'[docs/]', b'[a, a]', b'[./a]'):
            with self.subTest(bad=bad), self.assertRaises(hc.ContentError):
                hc.parse_role(ROLE.replace(b'effort: low\n', b'effort: low\nwrites: ' + bad + b'\n'))

    def test_quote_round_trip(self):
        for v in ('plain words', 'a: b', 'x #y', '"quoted"', 'back\\slash: x', '`tick` start'):
            with self.subTest(v=v):
                self.assertEqual(hc.unquote(hc.quote(v)), v)
        self.assertEqual(hc.quote('plain - words (ok)'), 'plain - words (ok)')
        self.assertEqual(hc.quote('go: now'), '"go: now"')


class SubstituteTest(unittest.TestCase):
    def sub(self, text, values=None):
        return hc.substitute(text, values or {'worktrees_dir': 'K'}, lambda n: 'p:' + n, ('beta',))

    def test_fills_every_token(self):
        self.assertEqual(self.sub('{{agent:beta}} at {{worktrees_dir}}/x'), 'p:beta at K/x')

    def test_refusals(self):
        for text in ('{{agent:gamma}}', '{{nonsense}}', '{{profile_path}}', '{{kit_root:x}}', '{{ kit_root }}'):
            with self.subTest(text=text):
                with self.assertRaises(hc.ContentError):
                    self.sub(text)

    def test_a_tiers_model_and_a_capabilitys_tool(self):
        fill = lambda text: hc.substitute(text, {}, str, (), models={'strong': 'big'},    # noqa: E731
                                          tools={'message': 'Tell'})
        self.assertEqual(fill('{{model:strong}} via {{tool:message}}'), 'big via Tell')
        for text in ('{{model:huge}}', '{{model:light}}', '{{model}}', '{{tool:teleport}}', '{{tool:read}}',
                     '{{tool}}'):
            with self.subTest(text=text), self.assertRaises(hc.ContentError):
                fill(text)
        with self.assertRaises(hc.ContentError):
            self.sub('{{model:strong}}')                # a host that passes no tier map


class ClaudeRendererTest(unittest.TestCase):
    def content(self, *roles):
        return hc.Content(roles=tuple(hc.parse_role(r) for r in roles), skills=())

    def test_agent_file(self):
        got = claude_content.renderer().render(self.content(ROLE, BETA))
        self.assertEqual(got['hosts/claude/shell/agents/alpha.md'],
                         b'---\nname: alpha\ndescription: Builds things. Ask pm-kit:beta first.\n'
                         b'tools: Read, Grep, Bash\nmodel: sonnet\neffort: low\n---\n\n'
                         b'Read .claude/agent-profile.md and CLAUDE.md; docs at <kit>/docs.\n')
        self.assertEqual(got['hosts/claude/shell/agents/beta.md'],
                         b'---\nname: beta\ndescription: "Two: parts"\nmodel: opus\n---\nbody\n')

    def test_a_mode_is_never_rendered(self):
        plain = claude_content.renderer().render(self.content(ROLE, BETA))
        moded = claude_content.renderer().render(self.content(
            ROLE.replace(b'effort: low\n', b'effort: low\nmode: write\nwrites: [docs/x.md]\n'), BETA.replace(b'tier: deep\n', b'tier: deep\nmode: read\n')))
        self.assertEqual(moded, plain)

    def test_long_cache_and_crlf(self):
        role = BETA.replace(b'capabilities: all\n', b'capabilities: all\ncache: long\n').replace(b'\n', b'\r\n')
        got = claude_content.renderer().render(self.content(role))['hosts/claude/shell/agents/beta.md']
        self.assertEqual(got, b'---\r\nname: beta\r\ndescription: "Two: parts"\r\nmodel: opus\r\n'
                              b'experimental:\r\n  cacheTtl: 1h\r\n---\r\nbody\r\n')

    def test_every_tier_and_capability_has_a_claude_value(self):
        self.assertEqual(sorted(claude_content.renderer().tier_models()), sorted(hc.TIERS))
        self.assertEqual(claude_content.renderer().tier_models(), dict(registry.host('claude').tiers))
        self.assertEqual(sorted(claude_content.TOOLS), sorted(hc.CAPABILITIES))
        self.assertEqual(sorted(claude_content.tokens()), sorted(t for t in hc.TOKENS if t not in hc.ARG_TOKENS))
        self.assertEqual(set(claude_content.TOKENS) & set(claude_content.PROFILE_TOKENS), set())

    def test_a_token_hosts_json_holds_is_read_from_it(self):
        """No Claude token value repeats a hosts.json field: PROFILE_TOKENS reads those."""
        host = registry.host('claude')
        p = host.profile
        values = claude_content.tokens()
        self.assertEqual({k: values[k] for k in claude_content.PROFILE_TOKENS},
                         {'worktrees_dir': p.worktrees_directory, 'owner_folder': '.claude/',
                          'user_folder': '~/' + p.state_directory, 'coauthor_email': p.attribution.email,
                          'long_context_model': 'the 1M-context %s model' % dict(host.tiers)['deep']})
        self.assertIn(values['owner_folder'], p.owner_paths)
        held = [v for v in (getattr(p, f) for f in p.__dataclass_fields__) if isinstance(v, str)]
        held += [p.attribution.email] + [m for _, m in host.tiers]
        for name, value in claude_content.TOKENS.items():
            with self.subTest(name):
                self.assertNotIn(value, held)


class TempTreeTest(unittest.TestCase):
    """drift and write on a copy of the kit's content/ and rendered folders."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        for rel in ('content', '.claude-plugin', os.path.join('hosts', 'claude', 'shell', 'agents'),
                    os.path.join('hosts', 'claude', 'shell', 'skills')):
            shutil.copytree(os.path.join(KIT_ROOT, rel), os.path.join(self.root, rel))
        for rel in ('kit.json', os.path.join('hosts', 'claude', 'shell', 'hooks', 'hooks.json')):
            write(self.path(rel), read(os.path.join(KIT_ROOT, rel)))
        self.rs = [claude_content.renderer()]

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def test_hand_edit_missing_and_stale_are_drift_and_write_repairs_them(self):
        worker = self.path('hosts', 'claude', 'shell', 'agents', 'worker.md')
        write(worker, read(worker) + b'hand edit\n')
        os.remove(self.path('hosts', 'claude', 'shell', 'agents', 'lander.md'))
        write(self.path('hosts', 'claude', 'shell', 'agents', 'ghost.md'), b'---\nname: ghost\n---\n')
        write(self.path('hosts', 'claude', 'shell', 'skills', 'ghost', 'SKILL.md'), b'x\n')
        self.assertEqual(content.drift(self.root, self.rs),
                         ['missing: hosts/claude/shell/agents/lander.md', 'differs: hosts/claude/shell/agents/worker.md',
                          'stale: hosts/claude/shell/agents/ghost.md', 'stale: hosts/claude/shell/skills/ghost/SKILL.md'])
        out = io.StringIO()
        self.assertEqual(cli.main(['--check', '-C', self.root], out), 1)
        self.assertIn('differs: hosts/claude/shell/agents/worker.md', out.getvalue())
        self.assertEqual(cli.main(['--write', '-C', self.root], io.StringIO()), 0)
        self.assertEqual(content.drift(self.root, self.rs), [])
        self.assertEqual(read(worker), read(os.path.join(KIT_ROOT, 'hosts', 'claude', 'shell', 'agents', 'worker.md')))
        self.assertFalse(os.path.exists(self.path('hosts', 'claude', 'shell', 'agents', 'ghost.md')))

    def test_a_role_edit_reaches_the_agent_file(self):
        role = self.path('content', 'roles', 'worker.md')
        write(role, read(role).replace(b'effort: medium', b'effort: high', 1))
        self.assertEqual(content.drift(self.root, self.rs), ['differs: hosts/claude/shell/agents/worker.md'])
        content.write(self.root, self.rs)
        self.assertIn(b'\neffort: high\r\n', read(self.path('hosts', 'claude', 'shell', 'agents', 'worker.md')))

    def test_a_kit_version_change_reaches_every_manifest(self):
        write(self.path('kit.json'), b'{"name": "pm-kit", "version": "9.8.7"}\n')
        self.assertEqual(content.drift(self.root, self.rs),
                         ['differs: .claude-plugin/marketplace.json', 'differs: .claude-plugin/plugin.json'])
        content.write(self.root, self.rs)
        self.assertEqual(json.loads(read(self.path('.claude-plugin', 'plugin.json')))['version'], '9.8.7')

    def test_a_bad_kit_json_is_usage(self):
        write(self.path('kit.json'), b'{"version": "1"}\n')
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', self.root], out), 2)
        self.assertIn('kit.json', out.getvalue())

    def test_a_bad_role_is_usage(self):
        role = self.path('content', 'roles', 'worker.md')
        write(role, read(role).replace(b'tier: standard', b'tier: huge', 1))
        out = io.StringIO()
        self.assertEqual(cli.main(['-C', self.root], out), 2)
        self.assertIn('content/roles/worker.md: tier', out.getvalue())

    def test_a_role_named_unlike_its_file_is_refused(self):
        os.rename(self.path('content', 'roles', 'worker.md'), self.path('content', 'roles', 'other.md'))
        with self.assertRaises(hc.ContentError):
            content.load(self.root)

    def test_unknown_host_is_usage(self):
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
            cli.main(['--host', 'nowhere'], io.StringIO())
        self.assertEqual(cm.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
