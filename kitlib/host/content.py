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
  mode: write                        optional: the role policy it is held to (MODES; docs/guard.md
                                     "Role policy"), where a repository turns that policy on.
                                     Read by the guard, never rendered into a host's file
  model.<host>: <model>              optional: that host runs this role on <model>, not the tier's

The body is neutral prose. Values that differ by host are tokens, `{{name}}` or
`{{agent:<role>}}` (TOKENS); `substitute` fills them from the host's own table. A role file keeps
its own line endings, and the body is copied byte for byte apart from its tokens.

A skill is a folder, `content/skills/<name>/`, whose files every host ships unchanged.
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
# The role-policy modes (kitlib.guard.rolepolicy, which takes them from here).
MODES = ('controller', 'write', 'read', 'test', 'lander')
ALL = 'all'
# The neutral capabilities. A host maps each to its own tool (or tools), keeping this order.
CAPABILITIES = (
    'read', 'edit', 'write', 'search', 'find', 'bash', 'powershell', 'worktree', 'artifact',
    'dispatch', 'message', 'list-agents', 'stop-task', 'monitor', 'tool-search', 'skill',
    'ask-owner', 'cron-create', 'cron-list', 'cron-delete', 'web-search', 'web-fetch',
)
# The tokens a body or description may hold. `agent` takes a role name: `{{agent:test-runner}}`.
TOKENS = ('agent', 'profile_path', 'coordinator_profile_path', 'instructions_file', 'kit_root',
          'worktrees_dir')
REQUIRED = ('name', 'description', 'tier', 'sandbox', 'capabilities')
OPTIONAL = ('effort', 'cache', 'mode')
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
    mode: str = None             # the role-policy mode (MODES); a host's file never carries it
    models: tuple = ()           # ((host, model), ...): per-host overrides of the tier's model
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
    for every file it writes. It reads nothing from disk and writes nothing."""

    host = None

    def tier_models(self):
        ...

    def owned(self):
        ...

    def render(self, content):
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
                         ('mode', MODES)):
        if key in fields and fields[key] not in allowed:
            raise ContentError('%s: %s %r is not one of %s' % (source, key, fields[key], ', '.join(allowed)))
    caps = _list(fields['capabilities'], '%s: capabilities' % source)
    for c in caps or ():
        if c not in CAPABILITIES:
            raise ContentError('%s: unknown capability %r' % (source, c))
    if caps is not None and len(set(caps)) != len(caps):
        raise ContentError('%s: a capability twice' % source)
    description = unquote(fields['description'], '%s: description' % source)
    if not description:
        raise ContentError('%s: empty description' % source)
    return Role(name=name, description=description, tier=fields['tier'], sandbox=fields['sandbox'],
                capabilities=caps or (), all_tools=caps is None, effort=fields.get('effort'),
                cache=fields.get('cache'), mode=fields.get('mode'), models=tuple(sorted(models.items())),
                body=text[m.end():], newline=newline)


def tokens_in(text):
    """[(name, argument or None)] of every token in `text`, in order."""
    return [(m.group(1), m.group(2)) for m in _TOKEN.finditer(text)]


def substitute(text, values, agent_name, roles, source='text'):
    """`text` with every token filled: `{{agent:<role>}}` by agent_name(<role>), any other by
    values[name]. A token outside TOKENS, an agent token naming no role in `roles`, a value the
    host does not define, or a `{{` left over is a ContentError."""
    def fill(m):
        name, arg = m.group(1), m.group(2)
        if name not in TOKENS:
            raise ContentError('%s: unknown token %s' % (source, m.group(0)))
        if name == 'agent':
            if arg not in roles:
                raise ContentError('%s: %s names no role' % (source, m.group(0)))
            return agent_name(arg)
        if arg is not None or name not in values:
            raise ContentError('%s: %s has no value on this host' % (source, m.group(0)))
        return values[name]
    out = _TOKEN.sub(fill, text)
    if '{{' in _TOKEN.sub('', text):
        raise ContentError('%s: a {{ that is not a token' % source)
    return out

