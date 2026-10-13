"""Codex's ContentRenderer (kitlib.host.content; docs/content.md "Codex").

Each role becomes `hosts/codex/shell/agents/kit-<name>.toml`, a Codex agent definition: a header
comment, then `name` (kit-<name>, hosts.json agent_name_format), `description`, `model` (the role's
`model.codex` override, else hosts.json's model for its tier; no line for ACCOUNT_DEFAULT, so the account's
configured model runs, as the Codex branch's roles.json had it), `model_reasoning_effort` (the
role's effort, else TIER_EFFORT of its tier), `sandbox_mode` (the role's `sandbox`) and
`developer_instructions`, the role body with its tokens filled from TOKENS, as a TOML multi-line
string. Codex agent files carry no tool list, so `capabilities` and `cache` are not rendered; the
sandbox is what limits a role. Lines end as the role file's do. Each skill folder is copied byte for
byte to `hosts/codex/shell/skills/<name>/`.

manifests(kit, content) writes the plugin's files, each JSON with indent 2 and a final newline:
- `hosts/codex/shell/plugin.json`: kit.json's name and version, the hooks file under
  extensions.com.openai (the portable plugin format the Codex branch used).
- `hosts/codex/shell/mcp.json`: the machine-lock MCP server, read-only on Codex
  (hosts/codex/shell/mcp/lock_server.py, hosts.codex.adapter.lock).
- `hosts/codex/shell/hooks/hooks.json`: every hosts.json hook event, each running the one entry
  hooks/run.py with the event's name. Codex reads a hook's command as one string, so it is
  kitlib.host.hookmain.manifest_command's command line joined, the script quoted.
vendored(root) copies the kit's boot check, boot/kitboot.py, to `hosts/codex/shell/boot/kitboot.py`
byte for byte: the plugin root is hosts/codex/shell (${PLUGIN_ROOT}), and a plugin copied out of the
kit must carry the check that verifies the kit it binds through PM_KIT_ROOT, as the Codex branch's
plugin carried release_binding.py. render_content --check fails when the copy drifts.
"""
import json
import os

from kitlib import agentname
from kitlib.host import hookmain, registry
from kitlib.host.content import ContentError, substitute

HOST = 'codex'
SHELL_REL = 'hosts/codex/shell'
AGENTS_REL = SHELL_REL + '/agents'
SKILLS_REL = SHELL_REL + '/skills'
MANIFEST_REL = SHELL_REL + '/plugin.json'
MCP_REL = SHELL_REL + '/mcp.json'
HOOKS_REL = SHELL_REL + '/hooks/hooks.json'
# Kit files the plugin ships a copy of: {copy: source}, both repo-relative.
VENDORED = {SHELL_REL + '/boot/kitboot.py': 'boot/kitboot.py'}
ENTRY = 'hooks/run.py'
PLUGIN_ROOT = '${PLUGIN_ROOT}'
PLUGIN_SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json'
MCP_SCHEMA = 'https://agent-plugins.org/schemas/1.0.0/mcp.schema.json'
DESCRIPTION = ('The PM kit as a Codex plugin (ARCHITECTURE.md, Packaging; hosts/codex/README.md): its hooks, '
               'skills and the read-only machine lock MCP server. Rendered from kit.json and content/ by '
               'tools/render_content.py; never edit it by hand.')
# (event, matcher, status message or None). Every event in hosts.json's capabilities, no other.
HOOKS = (
    ('SessionStart', 'startup|resume|clear|compact', 'Loading the PM kit header'),
    ('PostCompact', 'manual|auto', None),
    ('SubagentStart', '*', None),
    ('PreToolUse', '*', 'Checking the PM kit guard'),
)
HOOK_TIMEOUT = 10
MCP_SERVERS = {'machine-lock': {'type': 'stdio', 'command': 'python',
                                'args': [PLUGIN_ROOT + '/mcp/lock_server.py'],
                                'env_vars': ['PM_KIT_ROOT', 'PM_KIT_RELEASE_ID']}}
# hosts.json's Codex tiers are all ACCOUNT_DEFAULT: every role runs on the account's configured
# Codex model and differs by reasoning effort, as the Codex branch's roles.json had it. The owner
# names models per tier in hosts.json (docs/ROSTER.md) when Codex models are chosen.
ACCOUNT_DEFAULT = 'account-default'
TIER_EFFORT = {'light': 'low', 'standard': 'medium', 'strong': 'high', 'deep': 'xhigh'}


def agent_name(role):
    """The name Codex dispatches a kit role by: hosts.json's agent_name_format, kit-<role>."""
    return agentname.prefix(HOST) + role


# Codex's names for the kit's capabilities, as role prose names them ({{tool:<capability>}}). A
# Codex agent file carries no tool list, so these are prose only. INFERRED from the Codex branch's
# hook allowlist and native tool names, not from a captured session (README.md "Not verified");
# a capability Codex has no tool for says so.
TOOLS = {
    'read': 'read_file', 'search': 'Grep', 'find': 'Glob', 'edit': 'apply_patch', 'write': 'apply_patch',
    'bash': 'the shell tool', 'powershell': 'the shell tool',
    'worktree': 'python <kit>/tools/new_worktree.py', 'artifact': 'no Codex artifact tool',
    'dispatch': 'spawn_agent', 'message': 'send_message', 'list-agents': 'the PM\'s session list',
    'stop-task': 'no Codex task-stop tool', 'monitor': 'a background shell poll',
    'tool-search': 'no Codex tool search', 'skill': 'the skill', 'ask-owner': 'request_user_input',
    'cron-create': 'no Codex scheduler (ask the PM)', 'cron-list': 'no Codex scheduler (ask the PM)',
    'cron-delete': 'no Codex scheduler (ask the PM)', 'web-search': 'WebSearch', 'web-fetch': 'WebFetch',
}
# The tokens whose Codex value no hosts.json field holds.
TOKENS = {
    'profile_path': 'docs/agent-profile.md',
    'coordinator_profile_path': 'docs/coordinator-profile.md',
    'instructions_file': 'AGENTS.md',
    'config_dir_env': 'CODEX_HOME',
    # Plans only on Codex: the launcher runs nothing yet (launcher.py; README.md).
    'launch_agent': 'python <kit>/hosts/codex/shell/scripts/launch.py --role',
}
# The tokens read from hosts.json's Codex entry (a registry.Host), as every renderer reads them.
PROFILE_TOKENS = {
    'worktrees_dir': lambda host: host.profile.worktrees_directory,
    'owner_folder': lambda host: next(p for p in host.profile.owner_paths if p.endswith('/')),
    'user_folder': lambda host: '~/' + host.profile.state_directory,
    'coauthor_email': lambda host: host.profile.attribution.email,
    'long_context_model': lambda host: model_phrase(dict(host.tiers)['deep']),
}


def model_phrase(model):
    """How role prose names a Codex model: ACCOUNT_DEFAULT is the account's configured model."""
    return "the account's model" if model == ACCOUNT_DEFAULT else model


def tokens(registry_path=None):
    """Every one-value token's Codex value (docs/content.md "Tokens"): TOKENS, and PROFILE_TOKENS
    from hosts.json's Codex entry."""
    host = registry.host(HOST, registry_path)
    out = dict(TOKENS)
    out.update((name, value(host)) for name, value in PROFILE_TOKENS.items())
    return out


def prose_models():
    """{tier: how prose names its model} for {{model:<tier>}}."""
    return {t: model_phrase(m) for t, m in registry.host(HOST).tiers}


def toml_string(value):
    """`value` as a TOML basic string on one line."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\t':
            out.append('\\t')
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    return '"%s"' % ''.join(out)


def toml_block(value, nl):
    """`value` as a TOML multi-line basic string: its lines kept, `\\` and `"` escaped, and the
    newline after the opening quotes (which TOML drops) so the text starts on its own line."""
    out = []
    for ch in value:
        if ch in '"\\':
            out.append('\\' + ch)
        elif ch in '\n\r\t':
            out.append(ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7f:
            out.append('\\u%04x' % ord(ch))
        else:
            out.append(ch)
    return '"""' + nl + ''.join(out) + '"""'


def substitute_body(role, names):
    """The developer instructions of `role` on Codex: its body with the tokens filled, the newline
    that ends the front matter dropped. `names` are every role's name (for role tokens)."""
    src = '%s/kit-%s.toml' % (AGENTS_REL, role.name)
    body = substitute(role.body, tokens(), agent_name, names, src, models=prose_models(), tools=TOOLS)
    return body[len(role.newline):] if body.startswith(role.newline) else body


class CodexContent:
    host = HOST

    def tier_models(self):
        """hosts.json's Codex `tiers`: the one tier -> model map."""
        return dict(registry.host(HOST).tiers)

    def owned(self):
        return (AGENTS_REL, SKILLS_REL)

    def agent_file(self, role, names):
        """The bytes of `role`'s agent file; `names` are every role's name (for role tokens)."""
        src = '%s/kit-%s.toml' % (AGENTS_REL, role.name)
        values = tokens()
        fill = lambda text: substitute(text, values, agent_name, names, src, models=prose_models(),  # noqa: E731
                                       tools=TOOLS)
        model = role.model_for(HOST, self.tier_models())
        effort = role.effort or TIER_EFFORT[role.tier]
        if role.sandbox not in ('read-only', 'workspace-write'):
            raise ContentError('%s: no Codex sandbox_mode for %s' % (src, role.sandbox))
        nl = role.newline
        lines = ['# Rendered from content/roles/%s.md by tools/render_content.py; never edit it by hand.' % role.name,
                 '# Codex hooks do not see which agent runs: hosts/codex/README.md, "Not verified".',
                 'name = %s' % toml_string(agent_name(role.name)),
                 'description = %s' % toml_string(fill(role.description))]
        if model != ACCOUNT_DEFAULT:
            lines.append('model = %s' % toml_string(model))
        lines += ['model_reasoning_effort = %s' % toml_string(effort),
                  'sandbox_mode = %s' % toml_string(role.sandbox),
                  'developer_instructions = %s' % toml_block(substitute_body(role, names), nl)]
        return (nl.join(lines) + nl).encode('utf-8')

    def render(self, content):
        names = content.role_names()
        out = {}
        for role in content.roles:
            out['%s/kit-%s.toml' % (AGENTS_REL, role.name)] = self.agent_file(role, names)
        for skill in content.skills:
            for rel, data in skill.files:
                out['%s/%s/%s' % (SKILLS_REL, skill.name, rel)] = data
        return out

    @staticmethod
    def command_text(command):
        """A hookmain.manifest_command as one command string: each argument holding a space or a
        `$` quoted."""
        quote = lambda a: '"%s"' % a if any(c in a for c in ' $"') else a      # noqa: E731
        return ' '.join([command['command']] + [quote(a) for a in command['args']])

    def hooks(self, registry_path=None):
        entry = registry.host(HOST, registry_path)
        declared = set(entry.capabilities.hook_events)
        if declared != {e for e, _, _ in HOOKS}:
            raise ContentError('%s: HOOKS must cover exactly the hook events hosts.json declares' % HOOKS_REL)
        events = {}
        for event, matcher, status in HOOKS:
            command = hookmain.manifest_command('%s/%s' % (PLUGIN_ROOT, ENTRY), entry.platforms, event)
            hook = {'type': 'command', 'command': self.command_text(command), 'timeout': HOOK_TIMEOUT}
            if status:
                hook['statusMessage'] = status
            events[event] = [{'matcher': matcher, 'hooks': [hook]}]
        return {'hooks': events}

    def vendored(self, root):
        """{copy: bytes} of VENDORED, read from the kit checkout `root`."""
        out = {}
        for copy, source in VENDORED.items():
            with open(os.path.join(root, *source.split('/')), 'rb') as f:
                out[copy] = f.read()
        return out

    def manifests(self, kit, content, registry_path=None):
        """{path: bytes} of plugin.json, mcp.json and hooks.json for `kit` ({"name", "version"})."""
        plugin = {'$schema': PLUGIN_SCHEMA, 'name': kit['name'], 'version': kit['version'],
                  'description': DESCRIPTION, 'extensions': {'com.openai': {'hooks': './hooks/hooks.json'}}}
        mcp = {'$schema': MCP_SCHEMA, 'mcpServers': MCP_SERVERS}
        dump = lambda data: (json.dumps(data, indent=2) + '\n').encode('utf-8')     # noqa: E731
        return {MANIFEST_REL: dump(plugin), MCP_REL: dump(mcp), HOOKS_REL: dump(self.hooks(registry_path))}


def renderer():
    return CodexContent()
