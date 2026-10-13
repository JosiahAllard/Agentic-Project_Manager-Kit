"""Claude Code's ContentRenderer (kitlib.host.content; docs/content.md "Claude").

Each role becomes `claude/agents/<name>.md`: front matter `name`, `description`, `tools` (the
role's capabilities as Claude tool names, in order; no line for `capabilities: all`), `model`
(the role's `model.claude` override, else TIER_MODEL of its tier), `effort` when the role has one,
and for `cache: long` the `experimental` map with `cacheTtl: 1h`; then the body with its tokens
filled from TOKENS. Lines end as the role file's do. Each skill folder is copied byte for byte to
`claude/skills/<name>/`.
"""
from kitlib import agentname
from kitlib.host.content import ContentError, quote, substitute

HOST = 'claude'
AGENTS_REL = 'claude/agents'
SKILLS_REL = 'claude/skills'
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
    'kit_root': '${CLAUDE_PLUGIN_ROOT}',
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


def renderer():
    return ClaudeContent()
