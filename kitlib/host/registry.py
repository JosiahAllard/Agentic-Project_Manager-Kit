"""hosts.json, the host registry, read as data (ARCHITECTURE.md "Hosts").

  load(path)                  {name: Host}, the whole file checked; RegistryError on any fault
  names(path)                 the registered host names, in file order
  host(name, path)            one Host; RegistryError for a name the registry lacks
  default(path)               the one host marked "default": what an unset PM_KIT_HOST means
  selected(environ, path)     PM_KIT_HOST's host (blank or unset: the default), checked
  choose(flag, environ, path) a --host value when one was given, else selected()
  default_profile(path)       the default host's HostProfile
  profiles(path)              every host's HostProfile, in file order
  union(field, path)          a profile field's distinct values over every host, in file order
  host_folders(path)          the repo folder each host keeps its own files in (".claude")
  state_path(home, *parts)    a path in the kit's per-user state folder (~/.claude)

kitlib never imports a host's adapter. The registry hands out the adapter's module NAME, and the
entry point that wants the adapter imports it (tools/_host.py); hosts.json is data, so reading it
here keeps the layer rule. `path` defaults to PATH, the hosts.json at the kit root; tests pass
their own. Each call reads the file again: it is small, and a cache would outlive a test's file.

An entry: {"adapter": "<module>", "shell": "<repo path>", "platforms": ["windows", ...],
"capabilities": {...}, "profile": {...},
"tiers": {"light", "standard", "strong", "deep" -> model}} and, on exactly one host,
"default": true. kitlib.kitrelease.bootstrap reads "shell" itself, with json alone, because it
runs before kitlib is trusted.
"""
from dataclasses import dataclass
import json
import os
import re

from kitlib.host import Attribution, HostCapabilities, HostProfile


def kit_path(here=None):
    """The kit's hosts.json: beside the kitlib this module is in as it is spelled (kitroot.root()),
    else, when that has none, beside the folder it really is. A machine's installed lock tool runs
    kitlib through a junction (~/.claude/tools/kitlib -> <kit>/kitlib, docs/machine-setup.md
    "Links"), and the kit's hosts.json is beside the real kitlib, not beside the junction.
    `here` stands in for this file (tests)."""
    here = os.path.abspath(here or __file__)
    spelled = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(here))), 'hosts.json')
    if os.path.isfile(spelled):
        return spelled
    real = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(here)))),
                        'hosts.json')
    return real if os.path.isfile(real) else spelled


PATH = kit_path()
ENV = 'PM_KIT_HOST'
TIERS = ('light', 'standard', 'strong', 'deep')

_NAME = re.compile(r'[a-z][a-z0-9-]*\Z')
_ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_ENV_PREFIX = re.compile(r'[A-Z][A-Z0-9_]*_\Z')
_EMAIL = re.compile(r'[^\s<>@]+@[^\s<>@]+\Z')
_MODULE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*\Z')
_KEYS = {'adapter', 'shell', 'platforms', 'capabilities', 'profile', 'tiers'}
# The operating systems a host can declare it runs on (multi-host plan section 6). Windows is
# the only one the kit supports today; the field says so per host instead of the code assuming it.
PLATFORMS = ('windows', 'linux', 'macos')


class RegistryError(ValueError):
    """hosts.json is missing or malformed, or names no such host."""


@dataclass(frozen=True)
class Host:
    """One registry entry. `adapter` is a module name, never imported here; `tiers` is
    ((tier, model), ...) in TIERS order; `platforms` the PLATFORMS it runs on, in file order."""
    name: str
    adapter: str
    shell: str
    platforms: tuple
    capabilities: HostCapabilities
    profile: HostProfile
    tiers: tuple
    default: bool = False


def _pairs(items):
    out = {}
    for key, value in items:
        if key in out:
            raise RegistryError('hosts.json: duplicate key %r' % key)
        out[key] = value
    return out


def _text(where, value):
    if not isinstance(value, str) or not value.strip():
        raise RegistryError('hosts.json: %s must be a non-empty string' % where)
    return value


def _flag(where, value):
    if not isinstance(value, bool):
        raise RegistryError('hosts.json: %s must be true or false' % where)
    return value


def _texts(where, value):
    if not isinstance(value, list) or not value:
        raise RegistryError('hosts.json: %s must be a non-empty list of strings' % where)
    return tuple(_text(where, v) for v in value)


def _relpath(where, value):
    """A repo path: relative, forward slashes, no `.` or `..` step, so no entry points outside."""
    _text(where, value)
    parts = value.rstrip('/').split('/')
    if '\\' in value or ':' in value or value.startswith('/') or any(p in ('', '.', '..') for p in parts):
        raise RegistryError('hosts.json: %s must be a relative path inside the kit' % where)
    return value.rstrip('/')


def _object(where, value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise RegistryError('hosts.json: %s must have exactly the keys %s'
                            % (where, ', '.join(sorted(keys))))
    return value


def _capabilities(name, raw):
    w = '%s.capabilities' % name
    c = _object(w, raw, ('hook_events', 'supports_ask', 'subagent_identity_in_hooks', 'mcp',
                         'skills', 'agent_format'))
    return HostCapabilities(
        _texts(w + '.hook_events', c['hook_events']), _flag(w + '.supports_ask', c['supports_ask']),
        _flag(w + '.subagent_identity_in_hooks', c['subagent_identity_in_hooks']),
        _flag(w + '.mcp', c['mcp']), _flag(w + '.skills', c['skills']),
        _text(w + '.agent_format', c['agent_format']))


def _attribution(w, raw):
    a = _object(w, raw, ('name', 'email', 'model_env', 'model_pattern'))
    name, email = _text(w + '.name', a['name']), _text(w + '.email', a['email'])
    if name != ' '.join(name.split()) or not _EMAIL.match(email):
        raise RegistryError('hosts.json: %s needs a single-spaced name and a plain email' % w)
    model_env, pattern = a['model_env'], a['model_pattern']
    if model_env is not None and not (isinstance(model_env, str) and _ENV_NAME.match(model_env)):
        raise RegistryError('hosts.json: %s.model_env must be an environment variable name or null' % w)
    if pattern is not None:
        _text(w + '.model_pattern', pattern)
        try:
            re.compile(pattern)
        except re.error as e:
            raise RegistryError('hosts.json: %s.model_pattern is not a regex: %s' % (w, e)) from None
    return Attribution(name, email, model_env, pattern)


def _name_format(where, value):
    """An agent name format: {plugin} at most once, then {name} once and last, nothing else."""
    _text(where, value)
    head, sep, tail = value.partition('{name}')
    if not sep or tail or '{name}' in head or head.replace('{plugin}', '', 1).count('{') \
            or head.replace('{plugin}', '', 1).count('}'):
        raise RegistryError('hosts.json: %s must end in {name} and have no field but {plugin}'
                            % where)
    return value


def _env_prefixes(where, value):
    """Upper-case variable-name prefixes, each ending in `_` (CLAUDE_)."""
    prefixes = _texts(where, value)
    if not all(_ENV_PREFIX.match(p) for p in prefixes):
        raise RegistryError('hosts.json: %s must be upper-case prefixes ending in _' % where)
    return prefixes


def _profile(name, raw):
    w = '%s.profile' % name
    p = _object(w, raw, ('owner_paths', 'worktrees_directory', 'state_directory', 'process_names',
                         'mcp_tool_format', 'session_env', 'skills_directory', 'attribution',
                         'agent_name_format', 'session_process', 'env_prefixes'))
    session_env = _text(w + '.session_env', p['session_env'])
    if not _ENV_NAME.match(session_env):
        raise RegistryError('hosts.json: %s.session_env must be an environment variable name' % w)
    return HostProfile(
        _texts(w + '.owner_paths', p['owner_paths']),
        _relpath(w + '.worktrees_directory', p['worktrees_directory']),
        _text(w + '.state_directory', p['state_directory']),
        _texts(w + '.process_names', p['process_names']),
        _text(w + '.mcp_tool_format', p['mcp_tool_format']),
        session_env,
        _relpath(w + '.skills_directory', p['skills_directory']),
        _attribution(w + '.attribution', p['attribution']),
        _name_format(w + '.agent_name_format', p['agent_name_format']),
        _text(w + '.session_process', p['session_process']),
        _env_prefixes(w + '.env_prefixes', p['env_prefixes']))


def _entry(name, raw):
    if not isinstance(name, str) or not _NAME.match(name):
        raise RegistryError('hosts.json: host name %r must be lowercase letters, digits and -' % (name,))
    if not isinstance(raw, dict) or not _KEYS <= set(raw) <= _KEYS | {'default'}:
        raise RegistryError('hosts.json: %s must have the keys %s (and optionally default)'
                            % (name, ', '.join(sorted(_KEYS))))
    adapter = _text(name + '.adapter', raw['adapter'])
    if not _MODULE.match(adapter):
        raise RegistryError('hosts.json: %s.adapter must be a dotted module name' % name)
    tiers = _object(name + '.tiers', raw['tiers'], TIERS)
    platforms = _texts(name + '.platforms', raw['platforms'])
    if not set(platforms) <= set(PLATFORMS) or len(set(platforms)) != len(platforms):
        raise RegistryError('hosts.json: %s.platforms must name each of %s at most once'
                            % (name, ', '.join(PLATFORMS)))
    return Host(name, adapter, _relpath(name + '.shell', raw['shell']), platforms,
                _capabilities(name, raw['capabilities']), _profile(name, raw['profile']),
                tuple((t, _text('%s.tiers.%s' % (name, t), tiers[t])) for t in TIERS),
                _flag(name + '.default', raw.get('default', False)))


def load(path=None):
    """{name: Host} in file order. Refuses the whole file on any fault: a host half-read is worse
    than none, since a tool would act on the wrong worktree folder or attribution."""
    path = path or PATH
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8'), object_pairs_hook=_pairs)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        if isinstance(e, RegistryError):
            raise
        raise RegistryError('hosts.json cannot be read (%s): %s' % (path, e)) from None
    if not isinstance(data, dict) or not data:
        raise RegistryError('hosts.json must be an object of one or more hosts')
    hosts = {name: _entry(name, raw) for name, raw in data.items()}
    defaults = [h.name for h in hosts.values() if h.default]
    if len(defaults) != 1:
        raise RegistryError('hosts.json must mark exactly one host "default": true (found %d)'
                            % len(defaults))
    return hosts


def names(path=None):
    return tuple(load(path))


def host(name, path=None):
    hosts = load(path)
    if name not in hosts:
        raise RegistryError('unknown host %r: hosts.json has %s' % (name, ', '.join(hosts)))
    return hosts[name]


def default(path=None):
    return next(h.name for h in load(path).values() if h.default)


def selected(environ=None, path=None):
    """The host PM_KIT_HOST names in `environ` (default os.environ); blank or unset is the default
    host, so a session that sets nothing keeps today's behaviour. A name the registry lacks is
    refused, never guessed at."""
    environ = os.environ if environ is None else environ
    name = (environ.get(ENV) or '').strip()
    if not name:
        return default(path)
    return host(name, path).name


def choose(flag, environ=None, path=None):
    """A tool's --host value when given, else selected(): the one precedence every tool uses."""
    if flag is not None:
        return host(flag, path).name
    return selected(environ, path)


def profiles(path=None):
    """Every registered host's HostProfile, in file order: for core code that must know each host
    at once (the lock's agent-host test, the owner paths), not only the selected one."""
    return tuple(h.profile for h in load(path).values())


def union(field, path=None):
    """The distinct values of the HostProfile `field` over every host, in file order; the items of
    a tuple field each count. RegistryError as load(); AttributeError for a field it lacks."""
    out = []
    for profile in profiles(path):
        value = getattr(profile, field)
        out.extend(value if isinstance(value, tuple) else (value,))
    return tuple(dict.fromkeys(out))


def default_profile(path=None):
    """The default host's HostProfile: what a kit default that predates hosts stands for (the
    repo's worktrees_dir, the machine's kit state folder)."""
    return host(default(path), path).profile


def host_folders(path=None):
    """The top repo folder each host keeps its own files in (its settings, skills and agent
    worktrees), from the folder entries of its owner_paths ("x/"), each once, in file order:
    what a scan of a repository's own sources leaves out."""
    return tuple(dict.fromkeys(p.rstrip('/').split('/')[0]
                               for p in union('owner_paths', path) if p.endswith('/')))


def state_path(home, *parts, path=None):
    """`parts` under the kit's per-user state folder in `home`: the default host's profile
    state_directory, so it is one folder per machine whatever PM_KIT_HOST says, and the folder the
    kit has always used (~/.claude: the machine lock's store, the installed lock tool)."""
    return os.path.join(home, default_profile(path).state_directory, *parts)
