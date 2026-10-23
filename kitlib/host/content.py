"""The content contract (ARCHITECTURE.md "Content"; docs/multi-host-plan.md section 2.3): the
neutral model of a role and a skill as `content/` writes them, and the `ContentRenderer` protocol
each host's adapter implements to turn them into that host's files. Names no host.

A role file, `content/roles/<name>.md`, is front matter between two `---` lines, then the body:

  name: worker                       the file's stem
  description: ...                   one line; plain, or quoted "..." / '...' as YAML would need
  tier: standard                     light | standard | strong | deep (the host maps tier -> model)
  effort: medium                     optional: low | medium | high | xhigh | max
  sandbox: workspace-write           read-only | workspace-write
  capabilities: [read, edit, bash]   the tools the role gets, in order, or `all` for no list
  cache: long                        optional: the role asks for the host's long prompt cache
  compact: 170000                    optional: the role's auto-compact window in tokens (COMPACT
                                     bounds), a ceiling under the session's own; a host without a
                                     per-agent window renders nothing
  mode: write                        optional: the role policy it is held to (MODES; docs/guard.md
                                     "Role policy"), where a repository turns that policy on.
                                     Read by the guard, never rendered into a host's file
  writes: [docs/notes.local.md]      optional: repo-relative paths or globs (posix, no `..`) the
                                     role may write though its mode makes the checkout read-only
                                     (docs/guard.md "Role policy"). Never rendered either
  group: docs                        optional: the role's group (GROUPS) where its mode alone does
                                     not give it (MODE_GROUP); only with a mode the group allows
                                     (GROUP_MODES). Read by the backlog policy, never rendered
  run: session                       optional: the role runs only as its own launched session,
                                     never as a dispatched subagent (RUNS). Never rendered
  summary: ...                       optional: one line, the role's "use for" cell in the rendered
                                     docs/ROSTER.md (its description when absent). Never rendered
                                     into a host's file
  model.<host>: <model>              optional: that host runs this role on <model>, not the tier's

The body is neutral prose. Values that differ by host are tokens, `{{name}}`, `{{agent:<role>}}`,
`{{model:<tier>}}` or `{{tool:<capability>}}` (TOKENS); `substitute` fills them from the host's own
tables. A role file keeps its own line endings, and the body is copied byte for byte apart from its
tokens.

A skill is a folder, `content/skills/<name>/`, whose files every host ships unchanged.

`banner(source)` is the line every rendered file a comment can go in opens with (decision 19 in
docs/multi-host-plan.md section 7): the repo-relative file it is generated from, and the command
that renders it. Each renderer puts it in its file type's comment form; JSON takes none.
"""
import dataclasses
import re

try:                                    # typing.Protocol: Python 3.8+
    from typing import Protocol
except ImportError:                     # pragma: no cover
    Protocol = object

TIERS = ('light', 'standard', 'strong', 'deep')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
SANDBOXES = ('read-only', 'workspace-write')
CACHES = ('long',)
COMPACT = (100000, 1000000)   # a role's `compact:` bounds, the Claude CLI's own --autocompact range
# The role-policy modes (kitlib.guard.rolepolicy, which takes them from here).
MODES = ('controller', 'write', 'read', 'test', 'lander')
# A role's group (kitlib.backlog.roles reads them): its mode's (MODE_GROUP), or the `group:` it
# names, which must be one its mode allows (GROUP_MODES). A role with no mode has no group.
GROUPS = ('lead', 'builder', 'docs', 'read-only', 'lander')
MODE_GROUP = {'controller': 'lead', 'write': 'builder', 'read': 'read-only', 'test': 'read-only',
              'lander': 'lander'}
GROUP_MODES = {'lead': ('controller',), 'builder': ('write',), 'docs': ('write',),
               'read-only': ('read', 'test'), 'lander': ('lander',)}
RUNS = ('session',)
ALL = 'all'
# The neutral capabilities. A host maps each to its own tool (or tools), keeping this order.
CAPABILITIES = (
    'read', 'edit', 'write', 'search', 'find', 'bash', 'powershell', 'worktree', 'artifact',
    'dispatch', 'message', 'list-agents', 'stop-task', 'monitor', 'tool-search', 'skill',
    'ask-owner', 'cron-create', 'cron-list', 'cron-delete', 'web-search', 'web-fetch',
)
# The tokens a body or description may hold (docs/content.md "Tokens"). Three take an argument
# (ARG_TOKENS): `{{agent:<role>}}` the name the host dispatches a role by, `{{model:<tier>}}` the
# host's model for a tier (TIERS) and `{{tool:<capability>}}` the host's tool for a capability
# (CAPABILITIES). The rest are one value each on a host:
#   profile_path, coordinator_profile_path  a repository's agent and coordinator profiles
#   instructions_file   the repository instructions file the host reads
#   worktrees_dir       the repository folder agent worktrees go in
#   owner_folder        the host's own folder in a repository (owner-only), with its final /
#   user_folder         the host's per-user folder, home-relative (~/...)
#   config_dir_env      the environment variable that moves the host's per-user folder
#   long_context_model  a noun phrase for the model a launched long-context session runs on
#   coauthor_email      the address in the host's Co-Authored-By commit trailer
#   launch_agent        the command that starts the host's main session as a named agent; the
#                       agent's name ({{agent:<role>}}) follows it
ARG_TOKENS = ('agent', 'model', 'tool')
TOKENS = ARG_TOKENS + ('profile_path', 'coordinator_profile_path', 'instructions_file', 'worktrees_dir',
                       'owner_folder', 'user_folder', 'config_dir_env', 'long_context_model', 'coauthor_email',
                       'launch_agent')
REQUIRED = ('name', 'description', 'tier', 'sandbox', 'capabilities')
OPTIONAL = ('effort', 'cache', 'compact', 'mode', 'writes', 'group', 'run', 'summary')
MODEL_KEY = 'model.'

_FM = re.compile(r'\A---(\r?\n)(.*?)\r?\n---\r?\n', re.S)
_TOKEN = re.compile(r'\{\{([a-z_]+)(?::([a-z0-9-]+))?\}\}')
_NAME = re.compile(r'^[a-z][a-z0-9-]*\Z')


class ContentError(ValueError):
    """A role or skill file that does not have the shape this contract reads."""


@dataclasses.dataclass(frozen=True)
class Role:
    name: str
    description: str
    tier: str
    sandbox: str
    capabilities: tuple          # the neutral capabilities in order; () with all_tools
    all_tools: bool              # `capabilities: all`: the host gives every tool, no list
    effort: str = None
    cache: str = None
    compact: int = None          # COMPACT: the role's auto-compact window in tokens
    mode: str = None             # the role-policy mode (MODES); a host's file never carries it
    models: tuple = ()           # ((host, model), ...): per-host overrides of the tier's model
    writes: tuple = ()           # repo-relative posix paths or globs the role policy lets it write
    group: str = None            # GROUPS: the `group:` it names, else its mode's (MODE_GROUP)
    run: str = None              # RUNS: 'session' for a role only ever started as its own session
    summary: str = None          # its "use for" line in docs/ROSTER.md; never in a host's file
    body: str = ''
    newline: str = '\n'          # the role file's line ending; a host's file keeps it

    def model_for(self, host, tier_models):
        """The model `host` runs this role on: its override, else the host's model for the tier."""
        return dict(self.models).get(host) or tier_models[self.tier]


@dataclasses.dataclass(frozen=True)
class Skill:
    name: str
    files: tuple                 # ((path relative to the skill folder, posix, bytes), ...), sorted


@dataclasses.dataclass(frozen=True)
class Content:
    roles: tuple                 # Role, sorted by name
    skills: tuple                # Skill, sorted by name

    def role(self, name):
        for r in self.roles:
            if r.name == name:
                return r
        raise KeyError(name)

    def role_names(self):
        return tuple(r.name for r in self.roles)


class ContentRenderer(Protocol):
    """One host's renderer. `host` is the host's name. `tier_models()` is its tier -> model map.
    `owned()` lists the repo-relative folders whose every file it writes: a file there that
    `render` does not produce is stale. `render(content)` returns {repo-relative posix path: bytes}
    for every file it writes. It reads nothing from disk and writes nothing.

    What the host gives one role, read by tools/agent_view.py (docs/content.md "Agent view") and
    pinned to `render` by the conformance suite (tests/host/conformance.py):
    `tokens()` is {one-value token: its value on this host} (TOKENS less ARG_TOKENS; {} for a host
    whose files fill none). `tools()` is its capability -> tool map ({{tool:<capability>}}), or
    None when it has none. `role_path(name)` is the repo-relative path `render` writes role `name`
    to. `settings(role)` is [(key, value)]: what that file sets for the role to run with, each
    value as the file writes it; key `model` when the file names one, `tools` when it limits the
    role to a tool list (the host's tool names), nothing for what the file leaves to the host."""

    host = None

    def tier_models(self):
        ...

    def owned(self):
        ...

    def render(self, content):
        ...

    def tokens(self):
        ...

    def tools(self):
        ...

    def role_path(self, name):
        ...

    def settings(self, role):
        ...


def unquote(value, label='value'):
    """A one-line YAML scalar's text: "..." with \\" and \\\\ escapes, '...' with '' for ', or plain."""
    if value[:1] == '"':
        if len(value) < 2 or value[-1] != '"':
            raise ContentError('%s: quote not closed' % label)
        return re.sub(r'\\(.)', lambda m: m.group(1), value[1:-1])
    if value[:1] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise ContentError('%s: quote not closed' % label)
        return value[1:-1].replace("''", "'")
    return value


def needs_quotes(value):
    """True when a plain YAML scalar could not hold `value` on one line as it is."""
    return (': ' in value or ' #' in value or value.endswith(':') or value != value.strip()
            or value[:1] in '[]{}&*!|>%@`,\'"#?-' or not value)


def quote(value):
    """`value` as a one-line YAML scalar: plain when it can be, else double-quoted."""
    if not needs_quotes(value):
        return value
    return '"%s"' % value.replace('\\', '\\\\').replace('"', '\\"')


def _list(value, label):
    if value == ALL:
        return None
    if not (value.startswith('[') and value.endswith(']')):
        raise ContentError('%s: `all` or a [a, b] list, not %r' % (label, value))
    items = [v.strip() for v in value[1:-1].split(',') if v.strip()]
    return tuple(items)


def _writes(value, source):
    """The `writes` list: each a repo-relative posix path or glob, no `..`, no drive, no `\\`."""
    items = _list(value, '%s: writes' % source)
    if not items:
        raise ContentError('%s: writes: a [path, ...] list of at least one path' % source)
    for p in items:
        p = unquote(p, '%s: writes' % source)
        if (not p or p.startswith('/') or '\\' in p or ':' in p
                or any(part in ('', '.', '..') for part in p.split('/'))):
            raise ContentError('%s: writes: %r is not a repo-relative path' % (source, p))
    if len(set(items)) != len(items):
        raise ContentError('%s: writes: a path twice' % source)
    return tuple(unquote(p) for p in items)


def parse_role(data, source='role'):
    """The Role in the bytes `data` of a role file; ContentError naming `source` on any problem."""
    try:
        text = data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise ContentError('%s: not UTF-8: %s' % (source, e))
    m = _FM.match(text)
    if not m:
        raise ContentError('%s: no front matter between two --- lines' % source)
    newline = m.group(1)
    fields, models = {}, {}
    for line in m.group(2).splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(':')
        key, value = key.strip(), value.strip()
        if not sep or line[:1] in ' \t':
            raise ContentError('%s: not a `key: value` line: %r' % (source, line[:60]))
        if key.startswith(MODEL_KEY):
            host = key[len(MODEL_KEY):]
            if not _NAME.match(host) or not value:
                raise ContentError('%s: %s: a host name and a model' % (source, key))
            models[host] = unquote(value, key)
            continue
        if key not in REQUIRED + OPTIONAL:
            raise ContentError('%s: unknown key %s' % (source, key))
        if key in fields:
            raise ContentError('%s: %s twice' % (source, key))
        fields[key] = value
    missing = [k for k in REQUIRED if k not in fields]
    if missing:
        raise ContentError('%s: missing %s' % (source, ', '.join(missing)))
    name = fields['name']
    if not _NAME.match(name):
        raise ContentError('%s: name %r' % (source, name))
    for key, allowed in (('tier', TIERS), ('sandbox', SANDBOXES), ('effort', EFFORTS), ('cache', CACHES),
                         ('mode', MODES), ('group', GROUPS), ('run', RUNS)):
        if key in fields and fields[key] not in allowed:
            raise ContentError('%s: %s %r is not one of %s' % (source, key, fields[key], ', '.join(allowed)))
    caps = _list(fields['capabilities'], '%s: capabilities' % source)
    for c in caps or ():
        if c not in CAPABILITIES:
            raise ContentError('%s: unknown capability %r' % (source, c))
    if caps is not None and len(set(caps)) != len(caps):
        raise ContentError('%s: a capability twice' % source)
    writes = _writes(fields['writes'], source) if 'writes' in fields else ()
    description = unquote(fields['description'], '%s: description' % source)
    if not description:
        raise ContentError('%s: empty description' % source)
    mode, group = fields.get('mode'), fields.get('group')
    if group is not None and mode not in GROUP_MODES[group]:
        raise ContentError('%s: group %s needs mode %s' % (source, group, ' or '.join(GROUP_MODES[group])))
    compact = fields.get('compact')
    if compact is not None:
        if not re.match(r'^[0-9]+\Z', compact) or not COMPACT[0] <= int(compact) <= COMPACT[1]:
            raise ContentError('%s: compact %r is not a whole number of tokens %d..%d'
                               % ((source, compact) + COMPACT))
        compact = int(compact)
    summary = unquote(fields['summary'], '%s: summary' % source) if 'summary' in fields else None
    if summary is not None and not summary:
        raise ContentError('%s: empty summary' % source)
    return Role(name=name, description=description, tier=fields['tier'], sandbox=fields['sandbox'],
                capabilities=caps or (), all_tools=caps is None, effort=fields.get('effort'),
                cache=fields.get('cache'), compact=compact, mode=mode, models=tuple(sorted(models.items())),
                writes=writes, group=group or MODE_GROUP.get(mode), run=fields.get('run'), summary=summary,
                body=text[m.end():], newline=newline)


RENDER_COMMAND = 'python tools/render_content.py --write'
ROLE_SOURCE = 'content/roles/%s.md'


def banner(source):
    """The generated-file line for a file rendered from the repo-relative `source`, without the
    comment marker its file type adds."""
    return 'Generated from %s: edit that, then run %s. Never edit this file by hand.' % (source, RENDER_COMMAND)


def role_source(name):
    """The repo-relative role file role `name` is rendered from."""
    return ROLE_SOURCE % name


def tokens_in(text):
    """[(name, argument or None)] of every token in `text`, in order."""
    return [(m.group(1), m.group(2)) for m in _TOKEN.finditer(text)]


def substitute(text, values, agent_name, roles, source='text', models=None, tools=None):
    """`text` with every token filled: `{{agent:<role>}}` by agent_name(<role>), `{{model:<tier>}}`
    by models[<tier>] (the host's tier -> model map), `{{tool:<capability>}}` by tools[<capability>]
    (the host's capability -> tool name), any other by values[name]. A token outside TOKENS, an
    agent token naming no role in `roles`, a tier or capability the contract does not have, a
    value the host does not define, or a `{{` left over is a ContentError."""
    picks = {'model': (models, TIERS, 'tier'), 'tool': (tools, CAPABILITIES, 'capability')}

    def fill(m):
        name, arg = m.group(1), m.group(2)
        if name not in TOKENS:
            raise ContentError('%s: unknown token %s' % (source, m.group(0)))
        if name == 'agent':
            if arg not in roles:
                raise ContentError('%s: %s names no role' % (source, m.group(0)))
            return agent_name(arg)
        if name in picks:
            table, known, what = picks[name]
            if arg not in known:
                raise ContentError('%s: %s names no %s' % (source, m.group(0), what))
            if not table or arg not in table:
                raise ContentError('%s: %s has no value on this host' % (source, m.group(0)))
            return table[arg]
        if arg is not None or name not in values:
            raise ContentError('%s: %s has no value on this host' % (source, m.group(0)))
        return values[name]
    out = _TOKEN.sub(fill, text)
    if '{{' in _TOKEN.sub('', text):
        raise ContentError('%s: a {{ that is not a token' % source)
    return out

