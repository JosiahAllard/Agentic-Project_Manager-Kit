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

    def test_a_compact_window_is_not_rendered(self):
        # No per-agent compact key is established for Codex (docs/content.md "Codex").
        plain = self.render(ROLE, BETA)
        self.assertEqual(self.render(ROLE.replace(b'tier: deep\n', b'tier: deep\ncompact: 170000\n'), BETA), plain)

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

    def test_the_test_runner_works_in_its_given_worktree_and_never_creates_one(self):
        c = kcontent.load(ROOT)
        text = cc.renderer().render(c)['hosts/codex/shell/agents/kit-test-runner.toml'].decode('utf-8')
        self.assertNotIn('new_worktree.py', text)
        self.assertIn('run every command in that existing worktree (never create one', text)

    def test_no_rendered_role_names_the_other_hosts_values(self):
        from hosts.claude.adapter import content as claude
        c = kcontent.load(ROOT)
        files = cc.renderer().render(c)
        values = [v for v in claude.tokens().values()] + ['pm-kit:', 'CLAUDE.md']
        for path, data in files.items():
            if path.endswith('.toml'):
                for value in values:
                    self.assertNotIn(value.encode(), data, (path, value))


class DefaultRole(unittest.TestCase):
    def test_an_unmarked_session_is_the_read_type(self):
        """A Codex session with no PM_KIT_ROLE runs as the analyst, the read type (a role file the
        kit ships), never as a retired name."""
        from hosts.codex.adapter import kit
        self.assertEqual(kit.current_role({}), 'analyst')
        self.assertEqual(kit.catalog()['analyst']['sandbox'], 'read-only')


class Banner(unittest.TestCase):
    def test_every_committed_agent_file_opens_with_the_generated_line(self):
        """Its first line names the role file and the render command (decision 19 in
        docs/multi-host-plan.md section 7; docs/content.md "Generated files")."""
        folder = os.path.join(ROOT, *cc.AGENTS_REL.split('/'))
        names = kcontent.load(ROOT).role_names()
        self.assertEqual(sorted(n for n in os.listdir(folder) if n.endswith('.toml')),
                         sorted('kit-%s.toml' % n for n in names))
        for n in names:
            with self.subTest(role=n):
                with open(os.path.join(folder, 'kit-%s.toml' % n), encoding='utf-8') as f:
                    first = f.readline().rstrip('\r\n')
                self.assertEqual(first, '# ' + hc.banner(hc.role_source(n)))
                self.assertIn('python tools/render_content.py --write', first)


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
        # The manifest the export writes at .codex-plugin/plugin.json: every path from the plugin
        # root, the export (no $schema and no extensions: the live Codex refuses that form).
        self.assertEqual(sorted(plugin), ['description', 'hooks', 'mcpServers', 'name', 'skills', 'version'])
        self.assertEqual(plugin['skills'], './content/skills/')
        self.assertEqual(plugin['hooks'], './hosts/codex/shell/hooks/hooks.json')
        self.assertEqual(plugin['mcpServers'], './hosts/codex/shell/mcp.json')
        for key in ('skills', 'hooks', 'mcpServers'):
            self.assertTrue(os.path.exists(os.path.join(ROOT, *plugin[key][2:].strip('/').split('/'))), key)
        # Codex starts a plugin's MCP server in its cache copy and expands no ${PLUGIN_ROOT}.
        servers = json.loads(files[cc.MCP_REL])
        self.assertEqual(sorted(servers), ['mcpServers'])
        for name, script in (('machine-lock', 'lock_server.py'), ('hub', 'hub_server.py')):
            server = servers['mcpServers'][name]
            self.assertEqual(server['args'], ['hosts/codex/shell/mcp/' + script])
            self.assertEqual(server['cwd'], '.')
            self.assertTrue(os.path.isfile(os.path.join(ROOT, *server['args'][0].split('/'))))
        hooks = json.loads(files[cc.HOOKS_REL])['hooks']
        self.assertEqual(sorted(hooks), sorted(registry.host('codex').capabilities.hook_events))
        for event, groups in hooks.items():
            self.assertEqual(groups[0]['hooks'][0]['command'], 'pythonw "${PLUGIN_ROOT}/boot/hook.py" codex ' + event)

    def test_the_linux_hooks_are_one_posix_shell_line_each(self):
        """hooks.linux.json: the same events, matchers, timeout and status as hooks.json, each command
        /bin/sh running python3 on the plugin's hook entry; the guard's exits 2 when python3 fails
        (the guard-error deny), a start event's prints the did-not-load line."""
        files = cc.renderer().manifests(kcontent.load_kit(ROOT), kcontent.load(ROOT))
        self.assertEqual(cc.hooks_rel('linux'), 'hosts/codex/shell/hooks/hooks.linux.json')
        windows = json.loads(files[cc.HOOKS_REL])['hooks']
        linux = json.loads(files[cc.hooks_rel('linux')])['hooks']
        self.assertEqual(sorted(linux), sorted(windows))
        script = ' kit-hook "${PLUGIN_ROOT}/boot/hook.py" codex '
        start = ("/bin/sh -c 'python3 \"$@\" || echo \"PM kit: the kit did not load: python3 failed or is not "
                 "on PATH (exit $?)\"'")
        for event, groups in windows.items():
            (w,), (l,) = groups[0]['hooks'], linux[event][0]['hooks']
            self.assertEqual(linux[event][0]['matcher'], groups[0]['matcher'])
            self.assertEqual(dict(l, command=None), dict(w, command=None))
            head = "/bin/sh -c 'python3 \"$@\" || exit 2'" if event == 'PreToolUse' else start
            self.assertEqual(l['command'], head + script + event)

    def test_the_linux_mcp_servers_run_python3(self):
        """mcp.linux.json: mcp.json's servers, each run by python3 (stock Ubuntu has no `python`);
        mcp.json, the Windows file, keeps `python`. Nothing else differs."""
        files = cc.renderer().manifests(kcontent.load_kit(ROOT), kcontent.load(ROOT))
        windows = json.loads(files[cc.MCP_REL])
        linux = json.loads(files['hosts/codex/shell/mcp.linux.json'])
        self.assertEqual(sorted(linux['mcpServers']), ['hub', 'machine-lock'])
        for name, server in windows['mcpServers'].items():
            self.assertEqual(server['command'], 'python')
            self.assertEqual(linux['mcpServers'][name], dict(server, command='python3'))
        self.assertEqual(dict(linux, mcpServers=None), dict(windows, mcpServers=None))

    def test_a_linux_argument_quoting_cannot_hold_is_refused(self):
        self.assertEqual(cc.posix_quote('codex'), 'codex')
        self.assertEqual(cc.posix_quote('${PLUGIN_ROOT}/a b.py'), '"${PLUGIN_ROOT}/a b.py"')
        self.assertEqual(cc.posix_quote('a "$@" b'), "'a \"$@\" b'")
        for bad in ("it's", '${A}"', '${A}' + chr(92), '${A}' + chr(96)):
            with self.subTest(bad):
                with self.assertRaises(hc.ContentError):
                    cc.posix_quote(bad)

    def test_claude_and_codex_list_the_same_mcp_servers(self):
        files = cc.renderer().manifests(kcontent.load_kit(ROOT), kcontent.load(ROOT))
        codex = set(json.loads(files[cc.MCP_REL])['mcpServers'])
        with open(os.path.join(ROOT, '.claude-plugin', 'plugin.json'), encoding='utf-8') as f:
            claude = set(json.load(f)['mcpServers'])
        self.assertEqual(codex, claude)
        self.assertEqual(codex, {'machine-lock', 'hub'})

    def test_the_hook_events_must_match_hosts_json(self):
        r = cc.renderer()
        with unittest.mock.patch.object(cc, 'HOOKS', cc.HOOKS[:-1]):
            with self.assertRaises(hc.ContentError):
                r.hooks()


class Vendored(unittest.TestCase):
    def test_the_plugin_ships_the_kits_boot_folder_byte_for_byte_and_its_hosts_entry(self):
        files = cc.renderer().vendored(ROOT)
        want = {}
        for name in ('kitboot.py', 'hook.py'):
            with open(os.path.join(ROOT, 'boot', name), 'rb') as f:
                want['hosts/codex/shell/boot/' + name] = f.read()
        with open(os.path.join(ROOT, 'hosts.json'), encoding='utf-8') as f:
            entry = json.load(f)['codex']
        self.assertEqual(json.loads(files.pop(cc.HOSTS_REL)), {'codex': entry})
        self.assertEqual(files, want)
        rendered = kcontent.render(ROOT, [cc.renderer()])
        for path in list(want) + [cc.HOSTS_REL]:
            self.assertIn(path, rendered)

    def load_hook(self):
        """The plugin's copy of boot/hook.py, as a module."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'codex_hook', os.path.join(ROOT, 'hosts', 'codex', 'shell', 'boot', 'hook.py'))
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        return hook

    def test_the_guarded_tools_are_every_tool_the_policy_or_a_rule_judges(self):
        """The fast path skips a tool hosts.json's codex hook.guarded_tools leaves out, in the owner's
        unmarked session only (fast_path unmarked: a marked session's every call meets the policy's
        allowlist). What it skips is what the guard would allow: the read and bookkeeping tools."""
        from hosts.codex.adapter import events, policy
        spec = registry.host('codex').hook
        self.assertEqual(spec.fast_path, 'unmarked')
        judged = set(events.FILE_TOOLS) | set(events.SHELL_TOOLS) | {events.PATCH, events.STDIN}
        self.assertEqual(set(spec.guarded_tools), judged | {events.MCP + '*'})
        self.assertEqual([t for t in policy.READ_TOOLS | policy.BOOKKEEPING_TOOLS if spec.guarded(t)], [])
        from hosts.codex.adapter.host import ADAPTER
        self.assertLess(ADAPTER.DEADLINE, cc.HOOK_TIMEOUT, 'inside the timeout hooks.json gives Codex')

    def test_the_entrys_budget_counts_the_boot(self):
        """DEADLINE is the whole call's: the time the boot took (a pinned verify hashes every file)
        comes off what hookmain gets, which names the whole budget when it runs out."""
        import io
        import sys
        import types
        hook = self.load_hook()
        clock = [100.0]
        boot = types.ModuleType('kitboot')

        def slow_boot(here=None):
            clock[0] += 3.0
            return 'kit'
        boot.boot = slow_boot
        seen = {}

        def fake_run(adapter, event, stdin, stdout, environ, entry, deadline, started, args, kit):
            seen.update(deadline=deadline, started=started, args=args)
            return 0
        with unittest.mock.patch.dict(sys.modules, {'kitboot': boot}), \
                unittest.mock.patch('time.monotonic', lambda: clock[0]), \
                unittest.mock.patch('kitlib.host.hookmain.run', fake_run):
            rc = hook.main(['codex', 'PreToolUse'], io.StringIO('{"tool_name": "Write"}'), io.StringIO(), {})
        self.assertEqual(rc, 0)
        from hosts.codex.adapter.host import ADAPTER
        self.assertEqual(seen, {'deadline': ADAPTER.DEADLINE, 'started': 100.0, 'args': ['codex', 'PreToolUse']})

    def test_the_entrys_fast_path_reads_the_mark_as_sessionmark_does(self):
        """boot/hook.py reads the mark before the kit boots, so it cannot import kitlib.sessionmark:
        its MARK and test are held to sessionmark.ENV and sessionmark.mark's None."""
        import io
        import sys
        import types
        from kitlib import sessionmark
        hook = self.load_hook()
        self.assertEqual(hook.MARK, sessionmark.ENV)
        boot = types.ModuleType('kitboot')

        def no_boot(here=None):
            raise RuntimeError('booted')
        boot.boot = no_boot
        for value in ('', '   ', 'worker', ' reviewer ', 'owner', 'pm-kit:worker', 'pm-kit:'):
            with self.subTest(value=value), unittest.mock.patch.dict(sys.modules, {'kitboot': boot}):
                rc = hook.run(['codex', 'PreToolUse'], io.StringIO('{"tool_name": "Read"}'), io.StringIO(),
                              io.StringIO(), {sessionmark.ENV: value})
                self.assertEqual(rc, 0 if sessionmark.mark({sessionmark.ENV: value}) is None else 2)


import unittest.mock  # noqa: E402

if __name__ == '__main__':
    unittest.main()
