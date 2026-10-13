"""Claude Code's ContentRenderer (kitlib.host.content; docs/content.md "Claude").

Each role becomes `hosts/claude/shell/agents/<name>.md`: front matter `name`, `description`, `tools` (the
role's capabilities as Claude tool names, in order; no line for `capabilities: all`), `model`
(the role's `model.claude` override, else TIER_MODEL of its tier), `effort` when the role has one,
and for `cache: long` the `experimental` map with `cacheTtl: 1h`; then the body with its tokens
filled from TOKENS. Lines end as the role file's do. Each skill folder is copied byte for byte to
`hosts/claude/shell/skills/<name>/`.

manifests(kit, content) writes the files that carry the kit's one version (kit.json; ARCHITECTURE.md
"Packaging"), each JSON with indent 2 and a final newline:
- `.claude-plugin/plugin.json`: kit.json's name and version, every agent file, the skills folder,
  HOOKS_REL and the machine-lock MCP server. It stays at the kit root: the plugin root is the whole
  kit, since its hooks and MCP server run kitlib and tools beside them (${CLAUDE_PLUGIN_ROOT}).
- `.claude-plugin/marketplace.json`: the checkout as a local marketplace of that one plugin
  (`"source": "./"`), its version as `metadata.version`.
- HOOKS_REL: the plugin's hooks (HOOKS), each command line from kitlib.host.hookmain.manifest_command
  for the platforms hosts.json gives Claude, the script under ${CLAUDE_PLUGIN_ROOT}.
"""
import json

from kitlib import agentname
from kitlib.host import hookmain, registry
from kitlib.host.content import ContentError, quote, substitute

HOST = 'claude'
SHELL_REL = 'hosts/claude/shell'
AGENTS_REL = SHELL_REL + '/agents'
SKILLS_REL = SHELL_REL + '/skills'
HOOKS_REL = SHELL_REL + '/hooks/hooks.json'
MANIFEST_REL = '.claude-plugin/plugin.json'
MARKETPLACE_REL = '.claude-plugin/marketplace.json'
PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}'
DESCRIPTION = ('The PM kit as a Claude Code plugin (ARCHITECTURE.md, Packaging): the agents and skills '
               'rendered into hosts/claude/shell/, its hooks and the machine lock MCP server; templates, '
               'optional tools and knowledge ship beside them (docs/shipping.md). Rendered from kit.json '
               'and content/ by tools/render_content.py; never edit it by hand.')
# The plugin's hooks: (event, matcher or None, script in hooks/, its arguments). The kit header runs at
# every session and subagent start; the coordinator rules at a session's start, resume or compact.
HOOKS = (
    ('SessionStart', 'startup|resume|clear|compact', 'kit_header.py', ('SessionStart',)),
    ('SessionStart', 'startup|resume|compact', 'coordinator_session_start.py', ()),
    ('SubagentStart', None, 'kit_header.py', ('SubagentStart',)),
)
MCP_SERVERS = {'machine-lock': {'command': 'python', 'args': [PLUGIN_ROOT + '/mcp/lock_server.py']}}
TIER_MODEL = {'light': 'haiku', 'standard': 'sonnet', 'strong': 'opus', 'deep': 'opus'}
TOOLS = {
    'read': 'Read', 'edit': 'Edit', 'write': 'Write', 'search': 'Grep', 'find': 'Glob',
    'bash': 'Bash', 'powershell': 'PowerShell', 'worktree': 'EnterWorktree', 'artifact': 'Artifact',
    'dispatch': 'Agent', 'message': 'SendMessage', 'list-agents': 'ListAgents', 'stop-task': 'TaskStop',
    'monitor': 'Monitor', 'tool-search': 'ToolSearch', 'skill': 'Skill', 'ask-owner': 'AskUserQuestion',
    'cron-create': 'CronCreate', 'cron-list': 'CronList', 'cron-delete': 'CronDelete',
    'web-search': 'WebSearch', 'web-fetch': 'WebFetch',
}
TOKENS = {
    'profile_path': '.claude/agent-profile.md',
    'coordinator_profile_path': '.claude/coordinator-profile.md',
    'instructions_file': 'CLAUDE.md',
    'worktrees_dir': '.claude/worktrees',
}
CACHE_TTL = {'long': '1h'}


def agent_name(role):
    """The name Claude dispatches a kit role by: the plugin's prefix and the role."""
    return agentname.qualified(role)


class ClaudeContent:
    host = HOST

    def tier_models(self):
        return dict(TIER_MODEL)

    def owned(self):
        return (AGENTS_REL, SKILLS_REL)

    def agent_file(self, role, names):
        """The bytes of `role`'s agent file; `names` are every role's name (for agent tokens)."""
        src = '%s/%s.md' % (AGENTS_REL, role.name)
        fill = lambda text: substitute(text, TOKENS, agent_name, names, src)    # noqa: E731
        lines = ['---', 'name: %s' % role.name, 'description: %s' % quote(fill(role.description))]
        if not role.all_tools:
            missing = [c for c in role.capabilities if c not in TOOLS]
            if missing:
                raise ContentError('%s: no Claude tool for %s' % (src, ', '.join(missing)))
            lines.append('tools: %s' % ', '.join(TOOLS[c] for c in role.capabilities))
        lines.append('model: %s' % role.model_for(HOST, TIER_MODEL))
        if role.effort:
            lines.append('effort: %s' % role.effort)
        if role.cache:
            lines += ['experimental:', '  cacheTtl: %s' % CACHE_TTL[role.cache]]
        lines.append('---')
        nl = role.newline
        return (nl.join(lines) + nl + fill(role.body)).encode('utf-8')

    def render(self, content):
        names = content.role_names()
        out = {}
        for role in content.roles:
            out['%s/%s.md' % (AGENTS_REL, role.name)] = self.agent_file(role, names)
        for skill in content.skills:
            for rel, data in skill.files:
                out['%s/%s/%s' % (SKILLS_REL, skill.name, rel)] = data
        return out

    def hooks(self, registry_path=None):
        platforms = registry.host(HOST, registry_path).platforms
        events = {}
        for event, matcher, script, args in HOOKS:
            hook = {'type': 'command'}
            hook.update(hookmain.manifest_command('%s/%s/hooks/%s' % (PLUGIN_ROOT, SHELL_REL, script),
                                                  platforms, *args))
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
                         'description': 'The PM kit: agents, skills and the machine lock MCP server.'}],
        }
        dump = lambda data: (json.dumps(data, indent=2) + '\n').encode('utf-8')     # noqa: E731
        return {MANIFEST_REL: dump(plugin), MARKETPLACE_REL: dump(market),
                HOOKS_REL: dump(self.hooks(registry_path))}


def renderer():
    return ClaudeContent()
