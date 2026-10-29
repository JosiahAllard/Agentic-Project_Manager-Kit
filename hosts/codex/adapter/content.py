"""Codex's ContentRenderer (kitlib.host.content; docs/content.md "Codex").

Each role becomes `hosts/codex/shell/agents/kit-<name>.toml`, a Codex agent definition: a header
comment opening with the generated-file line (kitlib.host.content.banner), then `name` (kit-<name>,
hosts.json agent_name_format), `description`, `model` (the role's
`model.codex` override, else hosts.json's model for its tier; no line for ACCOUNT_DEFAULT, so the account's
configured model runs, as the Codex branch's roles.json had it), `model_reasoning_effort` (the
role's effort, else TIER_EFFORT of its tier), `sandbox_mode` (read-only for a read or lander
role, else the role's `sandbox`: sandbox_mode) and
`developer_instructions`, the role body with its tokens filled from TOKENS, as a TOML multi-line
string. Codex agent files carry no tool list, so `capabilities` and `cache` are not rendered; the
sandbox is what limits a role. Lines end as the role file's do. Each skill folder is copied byte for
byte to `hosts/codex/shell/skills/<name>/`: the default host's plugin points at `content/skills/`
itself, and Codex follows once a live install shows it loads skills from outside its plugin
folder (decision 29 in docs/multi-host-plan.md section 7; README.md "Not verified"). A skill file
is a copy, so it carries no generated-file line.

manifests(kit, content) writes the plugin's files, each JSON with indent 2 and a final newline:
- `hosts/codex/shell/plugin.json`: Codex's plugin manifest, which the plugin export
  (kitlib.pluginsource.export) writes, version stamped, at `.codex-plugin/plugin.json` of the
  export folder: the plugin root is the whole export, as Claude's is. kit.json's name and version,
  the skills (PLUGIN_SKILLS_REL, the kit's own `content/skills/`), the hooks file (HOOKS_REL) and the MCP
  servers file (MCP_REL), each a path from that root (docs/plugin-source.md "Codex").
- `hosts/codex/shell/mcp.json`: the machine-lock MCP server, read-only on Codex
  (hosts/codex/shell/mcp/lock_server.py, hosts.codex.adapter.lock), and the hub MCP server
  (hosts/codex/shell/mcp/hub_server.py, docs/hub-mcp.md), nested under mcpServers, each run with
  `cwd` "." and its script a path from the plugin root: Codex starts a plugin's MCP server in
  the plugin's folder in its cache and expands no ${PLUGIN_ROOT} in it. That is the Windows file; each other
  platform hosts.json gives Codex gets the same servers with that platform's MCP interpreter
  (mcp_servers) in mcp_rel(platform), `mcp.<platform>.json`, which the plugin export writes in its
  place.
- `hosts/codex/shell/hooks/hooks.json`: every hosts.json hook event, each running the kit's one
  hook entry, boot/hook.py, as ${PLUGIN_ROOT}/boot/hook.py (the plugin root: the export) with `codex` and the
  event's name. Codex reads a hook's command as one string, so it is
  kitlib.host.hookmain.manifest_command's command line joined, the script quoted (command_text).
  Each other platform hosts.json gives Codex gets the same hooks with that platform's command
  line, as a POSIX shell line, in hooks_rel(platform), `hooks/hooks.<platform>.json`; plugin.json
  names hooks.json alone, and the plugin export (kitlib.pluginsource) writes the platform's file
  in its place.
vendored(root) copies the kit's boot folder, boot/kitboot.py and boot/hook.py, to
`hosts/codex/shell/boot/` byte for byte, and writes `hosts/codex/shell/hosts.json` holding hosts.json's
Codex entry alone (HOSTS_REL), for the old plugin copy (hosts.codex.adapter.plugin_copy's mirror),
whose plugin root is hosts/codex/shell (${PLUGIN_ROOT}): a plugin copied out of the kit that way
cannot reach the kit's boot/ or hosts.json. An install from the export has the kit's own at its
root, and these copies go with the mirror (docs/plugin-source.md "Codex"). It must carry the
check that verifies the kit it binds through PM_KIT_ROOT, as the Codex branch's plugin carried
release_binding.py, the hook entry, and the hook data that entry reads before the kit boots (never
from the kit it is about to verify). render_content --check fails when a copy drifts.
The JSON files and the byte-for-byte copies carry no generated-file line: JSON holds no comment, and
a copy must stay the bytes of its source.
"""
import json
import os
import re

from kitlib import agentname
from kitlib.host import hookmain, registry
from kitlib.hubmcp import SERVER_NAME as HUB_NAME
from kitlib.lockmcp import SERVER_NAME
from kitlib.host.content import ContentError, banner, role_source, substitute

HOST = 'codex'
SHELL_REL = 'hosts/codex/shell'
AGENTS_REL = SHELL_REL + '/agents'
SKILLS_REL = SHELL_REL + '/skills'
MANIFEST_REL = SHELL_REL + '/plugin.json'
MCP_REL = SHELL_REL + '/mcp.json'
HOOKS_REL = SHELL_REL + '/hooks/hooks.json'
# Kit files the plugin ships a copy of: {copy: source}, both repo-relative.
VENDORED = {SHELL_REL + '/boot/kitboot.py': 'boot/kitboot.py', SHELL_REL + '/boot/hook.py': 'boot/hook.py'}
# The plugin's hosts.json: the kit's, Codex's entry alone, read by its copy of boot/hook.py.
HOSTS_REL = SHELL_REL + '/hosts.json'
ENTRY = 'boot/hook.py'
PLUGIN_ROOT = '${PLUGIN_ROOT}'
# The plugin's skills: the kit's content folder itself, as for Claude (Codex loads them from the
# plugin root, the export; the skills copy under SHELL_REL is the old mirror's, docs/plugin-source.md).
PLUGIN_SKILLS_REL = 'content/skills'
DESCRIPTION = ('The PM kit as a Codex plugin (ARCHITECTURE.md, Packaging; hosts/codex/README.md): its hooks, '
               'skills, the read-only machine lock MCP server and the hub MCP server. Rendered from kit.json and content/ by '
               'tools/render_content.py; never edit it by hand.')
# (event, matcher, status message or None). Every event in hosts.json's capabilities, no other.
HOOKS = (
    ('SessionStart', 'startup|resume|clear|compact', 'Loading the PM kit header'),
    ('PostCompact', 'manual|auto', None),
    ('SubagentStart', '*', None),
    ('PreToolUse', '*', 'Checking the PM kit guard'),
)
HOOK_TIMEOUT = 10
MCP_SERVERS = {SERVER_NAME: {'type': 'stdio', 'command': 'python',
                                'args': [SHELL_REL + '/mcp/lock_server.py'], 'cwd': '.',
                                'env_vars': ['PM_KIT_ROOT', 'PM_KIT_RELEASE_ID']},
               HUB_NAME: {'type': 'stdio', 'command': 'python',
                          'args': [SHELL_REL + '/mcp/hub_server.py'], 'cwd': '.',
                          'env_vars': ['PM_KIT_ROOT', 'PM_KIT_RELEASE_ID']}}


def mcp_servers(platform='windows'):
    """MCP_SERVERS, each command `platform`'s MCP interpreter (hookmain.mcp_interpreter)."""
    command = hookmain.mcp_interpreter(platform)
    return {name: dict(server, command=command) for name, server in MCP_SERVERS.items()}

# hosts.json's Codex tiers are all ACCOUNT_DEFAULT: every role runs on the account's configured
# Codex model and differs by reasoning effort, as the Codex branch's roles.json had it. The owner
# names models per tier in hosts.json (docs/ROSTER.md) when Codex models are chosen.
ACCOUNT_DEFAULT = 'account-default'
TIER_EFFORT = {'light': 'low', 'standard': 'medium', 'strong': 'high', 'deep': 'xhigh'}
# The role-policy modes that never get a writable Codex sandbox, whatever the role file's `sandbox`
# says for another host: a reader, and the lander (its landing execution is refused on Codex until
# reviewed), as the Codex branch's roles had them.
READ_ONLY_MODES = ('read', 'lander')


def sandbox_mode(role):
    """`role`'s Codex sandbox: read-only for READ_ONLY_MODES, else its role file's `sandbox`."""
    return 'read-only' if role.mode in READ_ONLY_MODES else role.sandbox


def agent_name(role):
    """The name Codex dispatches a kit role by: hosts.json's agent_name_format, kit-<role>."""
    return agentname.prefix(HOST) + role


# Codex's names for the kit's capabilities, as role prose names them ({{tool:<capability>}}). A
# Codex agent file carries no tool list, so these are prose only. INFERRED from the Codex branch's
# hook allowlist and native tool names, not from a captured session (README.md "Not verified").
# A capability Codex has no tool for, or whose tool hosts.codex.adapter.policy refuses a marked
# session (agent spawning and messaging), says so and points to the PM.
NOT_AVAILABLE = ' (not available to a Codex agent: ask the PM)'
TOOLS = {
    'read': 'read_file', 'search': 'Grep', 'find': 'Glob', 'edit': 'apply_patch', 'write': 'apply_patch',
    'bash': 'the shell tool', 'powershell': 'the shell tool',
    'worktree': 'python <kit>/tools/new_worktree.py -C <repo>',
    'enter-worktree': 'run every command in that existing worktree (never create one; pass `-C <worktree>` to kit tools)',
    'artifact': 'no Codex artifact tool',
    'dispatch': 'spawn_agent%s' % NOT_AVAILABLE, 'message': 'send_message%s' % NOT_AVAILABLE,
    'list-agents': 'the session list%s' % NOT_AVAILABLE, 'stop-task': 'task stop%s' % NOT_AVAILABLE,
    'monitor': 'a background shell poll', 'tool-search': 'tool search%s' % NOT_AVAILABLE,
    'skill': 'the skill', 'ask-owner': 'request_user_input',
    'cron-create': 'a schedule%s' % NOT_AVAILABLE, 'cron-list': 'the schedules%s' % NOT_AVAILABLE,
    'cron-delete': 'a schedule%s' % NOT_AVAILABLE, 'web-search': 'WebSearch', 'web-fetch': 'WebFetch',
}
# The tokens whose Codex value no hosts.json field holds.
TOKENS = {
    'profile_path': '.codex/agent-profile.md',
    'coordinator_profile_path': '.codex/coordinator-profile.md',
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


def tier_phrase(tier, model):
    """How role prose names a tier's model ({{model:<tier>}}): the model when hosts.json names one,
    else `<tier>-tier`, since every role then runs the account's model and a tier differs by
    reasoning effort ("a standard-tier worker", "pinned to standard-tier")."""
    return model if model != ACCOUNT_DEFAULT else '%s-tier' % tier


def tokens(registry_path=None):
    """Every one-value token's Codex value (docs/content.md "Tokens"): TOKENS, and PROFILE_TOKENS
    from hosts.json's Codex entry."""
    host = registry.host(HOST, registry_path)
    out = dict(TOKENS)
    out.update((name, value(host)) for name, value in PROFILE_TOKENS.items())
    return out


def prose_models():
    """{tier: how prose names its model} for {{model:<tier>}}."""
    return {t: tier_phrase(t, m) for t, m in registry.host(HOST).tiers}


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

    @staticmethod
    def tokens():
        return tokens()

    @staticmethod
    def tools():
        return dict(TOOLS)

    @staticmethod
    def role_path(name):
        return '%s/kit-%s.toml' % (AGENTS_REL, name)

    def settings(self, role):
        """[(key, value)] of what `role`'s agent file runs it with, in the file's order: `model`
        (none for ACCOUNT_DEFAULT: the account's model runs), `model_reasoning_effort` and
        `sandbox_mode`. No tool list: a Codex agent file has none."""
        model = role.model_for(HOST, self.tier_models())
        if sandbox_mode(role) not in ('read-only', 'workspace-write'):
            raise ContentError('%s: no Codex sandbox_mode for %s' % (self.role_path(role.name), role.sandbox))
        out = [('model', model)] if model != ACCOUNT_DEFAULT else []
        return out + [('model_reasoning_effort', role.effort or TIER_EFFORT[role.tier]),
                      ('sandbox_mode', sandbox_mode(role))]

    def agent_file(self, role, names):
        """The bytes of `role`'s agent file; `names` are every role's name (for role tokens)."""
        src = self.role_path(role.name)
        values = tokens()
        fill = lambda text: substitute(text, values, agent_name, names, src, models=prose_models(),  # noqa: E731
                                       tools=TOOLS)
        nl = role.newline
        lines = ['# ' + banner(role_source(role.name)),
                 '# Review template: do not install until live acceptance (hosts/codex/README.md).',
                 '# Codex hooks do not see which agent runs: hosts/codex/README.md, "Not verified".',
                 'name = %s' % toml_string(agent_name(role.name)),
                 'description = %s' % toml_string(fill(role.description))]
        lines += ['%s = %s' % (key, toml_string(value)) for key, value in self.settings(role)]
        lines.append('developer_instructions = %s' % toml_block(substitute_body(role, names), nl))
        return (nl.join(lines) + nl).encode('utf-8')

    def render(self, content):
        names = content.role_names()
        out = {}
        for role in content.roles:
            out[self.role_path(role.name)] = self.agent_file(role, names)
        for skill in content.skills:
            for rel, data in skill.files:
                out['%s/%s/%s' % (SKILLS_REL, skill.name, rel)] = data
        return out

    @staticmethod
    def command_text(command, platform='windows'):
        """A hookmain.manifest_command as one command string. Windows: each argument holding a
        space or a `$` in double quotes. Elsewhere a POSIX shell line: an argument naming a
        variable (`${`) in double quotes, so the shell or Codex fills it; any other argument
        that is not plain in single quotes, so `$@` and `$?` reach the /bin/sh it starts."""
        if platform == 'windows':
            quote = lambda a: '"%s"' % a if any(c in a for c in ' $"') else a      # noqa: E731
        else:
            quote = posix_quote
        return ' '.join([command['command']] + [quote(a) for a in command['args']])

    def hooks(self, registry_path=None, platform='windows'):
        entry = registry.host(HOST, registry_path)
        declared = set(entry.capabilities.hook_events)
        if declared != {e for e, _, _ in HOOKS}:
            raise ContentError('%s: HOOKS must cover exactly the hook events hosts.json declares' % HOOKS_REL)
        events = {}
        for event, matcher, status in HOOKS:
            command = hookmain.manifest_command('%s/%s' % (PLUGIN_ROOT, ENTRY), platform, HOST, event,
                                                blocking=event == entry.hook.tool_event)
            hook = {'type': 'command', 'command': self.command_text(command, platform), 'timeout': HOOK_TIMEOUT}
            if status:
                hook['statusMessage'] = status
            events[event] = [{'matcher': matcher, 'hooks': [hook]}]
        return {'hooks': events}

    def vendored(self, root):
        """{copy: bytes} of VENDORED, read from the kit checkout `root`, and HOSTS_REL: its
        hosts.json's Codex entry alone, JSON with indent 2 and a final newline."""
        out = {}
        for copy, source in VENDORED.items():
            with open(os.path.join(root, *source.split('/')), 'rb') as f:
                out[copy] = f.read()
        with open(os.path.join(root, 'hosts.json'), encoding='utf-8') as f:
            entry = json.load(f)[HOST]
        out[HOSTS_REL] = (json.dumps({HOST: entry}, indent=2) + '\n').encode('utf-8')
        return out

    def manifests(self, kit, content, registry_path=None):
        """{path: bytes} of plugin.json, and mcp.json and hooks.json once for each platform
        hosts.json gives Codex, for `kit` ({"name", "version"})."""
        plugin = {'name': kit['name'], 'version': kit['version'], 'description': DESCRIPTION,
                  'skills': './%s/' % PLUGIN_SKILLS_REL, 'hooks': './' + HOOKS_REL, 'mcpServers': './' + MCP_REL}
        dump = lambda data: (json.dumps(data, indent=2) + '\n').encode('utf-8')     # noqa: E731
        out = {MANIFEST_REL: dump(plugin)}
        for platform in registry.host(HOST, registry_path).platforms:
            out[mcp_rel(platform)] = dump({'mcpServers': mcp_servers(platform)})
            out[hooks_rel(platform)] = dump(self.hooks(registry_path, platform))
        return out


def mcp_rel(platform):
    """The MCP servers file for `platform`: MCP_REL on Windows, the one the plugin reads; beside it
    `mcp.<platform>.json` elsewhere, which kitlib.pluginsource.export writes in its place."""
    return MCP_REL if platform == 'windows' else MCP_REL[:-len('.json')] + '.%s.json' % platform


def hooks_rel(platform):
    """The hooks file for `platform`: HOOKS_REL on Windows, the one plugin.json names; beside it
    `hooks.<platform>.json` elsewhere, which kitlib.pluginsource.export writes in its place."""
    return HOOKS_REL if platform == 'windows' else HOOKS_REL[:-len('.json')] + '.%s.json' % platform


PLAIN = re.compile(r'[A-Za-z0-9_./:=@%+,-]+\Z')


def posix_quote(arg):
    """`arg` as one POSIX shell word: plain as is, a `${NAME}` reference in double quotes, any
    other in single quotes. ContentError for a form that quoting would change."""
    if PLAIN.match(arg):
        return arg
    if '${' in arg:
        if any(c in arg for c in '"\\`'):
            raise ContentError('%s: cannot double-quote the hook argument %r' % (HOOKS_REL, arg))
        return '"%s"' % arg
    if "'" in arg:
        raise ContentError('%s: cannot single-quote the hook argument %r' % (HOOKS_REL, arg))
    return "'%s'" % arg


def renderer():
    return CodexContent()
