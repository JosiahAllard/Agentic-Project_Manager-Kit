"""Claude Code's ContentRenderer (kitlib.host.content; docs/content.md "Claude").

Each role becomes `hosts/claude/shell/agents/<name>.md`: front matter opening with the generated-file
line as a YAML comment (kitlib.host.content.banner; the front matter stays the file's first thing),
then `name`, `description`, `tools` (the
role's capabilities as Claude tool names, in order; no line for `capabilities: all`), `model`
(the role's `model.claude` override, else hosts.json's `tiers` model for its tier: tier_models), `effort`
when the role has one, and for `cache: long` the `experimental` map with `cacheTtl: 1h`; then the
body with its tokens filled from tokens(): TOKENS, and PROFILE_TOKENS read from hosts.json's Claude
entry, so a value hosts.json holds is written once; `{{model:<tier>}}` from tier_models() and
`{{tool:<capability>}}` from TOOLS, the map the `tools` line uses. Lines end as the role file's do.
Skills are not copied: the plugin manifest points at `content/skills/` itself (decision 29 in
docs/multi-host-plan.md section 7).

manifests(kit, content) writes the files that carry the kit's one version (kit.json; ARCHITECTURE.md
"Packaging"), each JSON with indent 2 and a final newline:
- `.claude-plugin/plugin.json`: kit.json's name and version, every agent file, SKILLS_REL (the
  kit's own `content/skills/`), HOOKS_REL and the MCP servers (MCP_SERVERS: the machine lock's and the hub's). It stays at the kit
  root: the plugin root is the whole kit, since its hooks and MCP server run kitlib and tools beside them (${CLAUDE_PLUGIN_ROOT}).
- `.claude-plugin/marketplace.json`: the checkout as a local marketplace of that one plugin
  (`"source": "./"`), its version as `metadata.version`.
- HOOKS_REL: the plugin's hooks (HOOKS), each command line from kitlib.host.hookmain.manifest_command
  for the platforms hosts.json gives Claude: the kit's one hook entry, ${CLAUDE_PLUGIN_ROOT}/ENTRY,
  with `claude`, the event and, for the coordinator rules, its part (boot/hook.py).
JSON holds no comment, and these keep to the keys Claude Code documents, so they carry no
generated-file line; plugin.json's description says it is rendered.
"""
import json

from kitlib import agentname
from kitlib.host import hookmain, registry
from kitlib.host.content import ContentError, banner, quote, role_source, substitute

HOST = 'claude'
SHELL_REL = 'hosts/claude/shell'
AGENTS_REL = SHELL_REL + '/agents'
SKILLS_REL = 'content/skills'                  # the plugin's skills: the content folder itself
RETIRED_SKILLS_REL = SHELL_REL + '/skills'     # the skills' copy before decision 29: any file there is stale
HOOKS_REL = SHELL_REL + '/hooks/hooks.json'
MANIFEST_REL = '.claude-plugin/plugin.json'
MARKETPLACE_REL = '.claude-plugin/marketplace.json'
PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}'
DESCRIPTION = ('The PM kit as a Claude Code plugin (ARCHITECTURE.md, Packaging): the agents rendered into '
               'hosts/claude/shell/agents/, the skills in content/skills/, its hooks and the machine lock and hub '
               'MCP servers; templates, optional tools and knowledge ship beside them (docs/shipping.md). '
               'Rendered from kit.json and content/ by tools/render_content.py; never edit it by hand.')
ENTRY = 'boot/hook.py'
# The plugin's hooks: (event, matcher or None, the part boot/hook.py runs or None). The kit header
# runs at every session and subagent start; the coordinator rules at a session's start, resume or
# compact. The guard (PreToolUse) and the PM checklist are in settings (kitlib.machinesetup).
HOOKS = (
    ('SessionStart', 'startup|resume|clear|compact', None),
    ('SessionStart', 'startup|resume|compact', 'coordinator'),
    ('SubagentStart', None, None),
)
MCP_SERVERS = {'machine-lock': {'command': 'python', 'args': [PLUGIN_ROOT + '/mcp/lock_server.py']},
               'hub': {'command': 'python', 'args': [PLUGIN_ROOT + '/mcp/hub_server.py']}}
TOOLS = {
    'read': 'Read', 'edit': 'Edit', 'write': 'Write', 'search': 'Grep', 'find': 'Glob',
    'bash': 'Bash', 'powershell': 'PowerShell', 'worktree': 'EnterWorktree', 'artifact': 'Artifact',
    'dispatch': 'Agent', 'message': 'SendMessage', 'list-agents': 'ListAgents', 'stop-task': 'TaskStop',
    'monitor': 'Monitor', 'tool-search': 'ToolSearch', 'skill': 'Skill', 'ask-owner': 'AskUserQuestion',
    'cron-create': 'CronCreate', 'cron-list': 'CronList', 'cron-delete': 'CronDelete',
    'web-search': 'WebSearch', 'web-fetch': 'WebFetch',
}
# The tokens whose Claude value no hosts.json field holds.
TOKENS = {
    'profile_path': '.claude/agent-profile.md',
    'coordinator_profile_path': '.claude/coordinator-profile.md',
    'instructions_file': 'CLAUDE.md',
    'config_dir_env': 'CLAUDE_CONFIG_DIR',
    'launch_agent': 'claude --agent',
}
# The tokens read from hosts.json's Claude entry (a registry.Host): token -> its value from it.
PROFILE_TOKENS = {
    'worktrees_dir': lambda host: host.profile.worktrees_directory,
    'owner_folder': lambda host: next(p for p in host.profile.owner_paths if p.endswith('/')),
    'user_folder': lambda host: '~/' + host.profile.state_directory,
    'coauthor_email': lambda host: host.profile.attribution.email,
    'long_context_model': lambda host: 'the 1M-context %s model' % dict(host.tiers)['deep'],
}
CACHE_TTL = {'long': '1h'}


def tokens(registry_path=None):
    """Every one-value token's Claude value: TOKENS, and PROFILE_TOKENS from hosts.json's Claude
    entry."""
    host = registry.host(HOST, registry_path)
    out = dict(TOKENS)
    out.update((name, value(host)) for name, value in PROFILE_TOKENS.items())
    return out


def agent_name(role):
    """The name Claude dispatches a kit role by: the plugin's prefix and the role."""
    return agentname.qualified(role)


class ClaudeContent:
    host = HOST

    def tier_models(self):
        """hosts.json's Claude `tiers`: the one tier -> model map."""
        return dict(registry.host(HOST).tiers)

    def owned(self):
        return (AGENTS_REL,)

    def retired(self):
        """Folders this renderer once wrote and no longer does (kitlib.content: every file in
        one is stale)."""
        return (RETIRED_SKILLS_REL,)

    @staticmethod
    def tokens():
        return tokens()

    @staticmethod
    def tools():
        return dict(TOOLS)

    @staticmethod
    def role_path(name):
        return '%s/%s.md' % (AGENTS_REL, name)

    def settings(self, role, tiers=None):
        """[(key, value)] of the front matter `role`'s agent file runs it with, in the file's order:
        `tools` (no line for `capabilities: all`), `model`, `effort`, `autoCompactWindow` (the role's
        `compact`; the CLI holds a subagent's window at or under it), and `cacheTtl` (under
        `experimental`)."""
        tiers = self.tier_models() if tiers is None else tiers
        out = []
        if not role.all_tools:
            missing = [c for c in role.capabilities if c not in TOOLS]
            if missing:
                raise ContentError('%s: no Claude tool for %s' % (self.role_path(role.name), ', '.join(missing)))
            out.append(('tools', ', '.join(TOOLS[c] for c in role.capabilities)))
        out.append(('model', role.model_for(HOST, tiers)))
        if role.effort:
            out.append(('effort', role.effort))
        if role.compact:
            out.append(('autoCompactWindow', str(role.compact)))
        if role.cache:
            out.append(('cacheTtl', CACHE_TTL[role.cache]))
        return out

    def agent_file(self, role, names, values=None, tiers=None):
        """The bytes of `role`'s agent file; `names` are every role's name (for agent tokens),
        `values` the tokens' values and `tiers` the tier -> model map (default: hosts.json's)."""
        src = self.role_path(role.name)
        values = tokens() if values is None else values
        tiers = self.tier_models() if tiers is None else tiers
        fill = lambda text: substitute(text, values, agent_name, names, src, models=tiers,    # noqa: E731
                                       tools=TOOLS)
        lines = ['---', '# ' + banner(role_source(role.name)), 'name: %s' % role.name,
                 'description: %s' % quote(fill(role.description))]
        for key, value in self.settings(role, tiers):
            lines += ['experimental:', '  cacheTtl: %s' % value] if key == 'cacheTtl' else ['%s: %s' % (key, value)]
        lines.append('---')
        nl = role.newline
        return (nl.join(lines) + nl + fill(role.body)).encode('utf-8')

    def render(self, content):
        names = content.role_names()
        values, tiers = tokens(), self.tier_models()
        out = {}
        for role in content.roles:
            out[self.role_path(role.name)] = self.agent_file(role, names, values, tiers)
        return out

    def hooks(self, registry_path=None):
        platforms = registry.host(HOST, registry_path).platforms
        events = {}
        for event, matcher, part in HOOKS:
            hook = {'type': 'command'}
            hook.update(hookmain.manifest_command('%s/%s' % (PLUGIN_ROOT, ENTRY), platforms, HOST, event,
                                                  *([part] if part else [])))
            group = {'matcher': matcher} if matcher else {}
            group['hooks'] = [hook]
            events.setdefault(event, []).append(group)
        return {'hooks': events}

    def manifests(self, kit, content, registry_path=None):
        """{path: bytes} of the plugin manifest, the marketplace and the hooks file, for `kit`
        ({"name", "version"}, kitlib.content.load_kit) and `content`."""
        plugin = {
            'name': kit['name'], 'version': kit['version'], 'description': DESCRIPTION,
            'agents': ['./%s/%s.md' % (AGENTS_REL, n) for n in sorted(content.role_names())],
            'skills': ['./%s/' % SKILLS_REL], 'hooks': './' + HOOKS_REL, 'mcpServers': MCP_SERVERS,
        }
        market = {
            'name': kit['name'],
            'description': "The PM kit's plugin, loaded in place from a kit checkout (docs/install.md, "
                           'Plugin install).',
            'owner': {'name': 'PM kit'}, 'metadata': {'version': kit['version']},
            'plugins': [{'name': kit['name'], 'source': './',
                         'description': 'The PM kit: agents, skills and the machine lock and hub MCP servers.'}],
        }
        dump = lambda data: (json.dumps(data, indent=2) + '\n').encode('utf-8')     # noqa: E731
        return {MANIFEST_REL: dump(plugin), MARKETPLACE_REL: dump(market),
                HOOKS_REL: dump(self.hooks(registry_path))}


def renderer():
    return ClaudeContent()
