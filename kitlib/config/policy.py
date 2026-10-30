"""Kit policy read from config: heavy programs, roles, the kinds table, and the repo hook module.

Heavy programs (ARCHITECTURE.md "Kinds": a program is heavy because it is on the heavy list).
  HEAVY_DEFAULTS are the kit's (on Linux LINUX_HEAVY_DEFAULTS too); `.kit.json` "heavy".programs
  adds a repo's own (it cannot remove a default). heavy_match(program, args, entries) is the first
  entry whose `program` glob matches the program's basename (case-insensitive), whose `when_args`
  (if any) has one argument present, each set in whose `also_args` (kit defaults only) has one
  too, and whose `unless_args` has none. An argument matches by itself or as `<arg>=<value>`. Finding
  the program in a command line (interpreters, shells, Start-Process) is the guard's job.

Roles. role_name(action) is `main` for a kitlib.host.Action of the main session (no `agent`),
  else its `agent_role` when the roles table names it, else `subagent`. The host adapter gives
  agent_role in the kit's naming: Claude's adapter drops exactly the kit plugin's prefix
  (`<plugin>:lander` is `lander`; kitlib.agentname.bare), and any other form names no role.
  permissions(action, roles) is that role's entry over its base (`main` or `subagent`) over
  ROLE_DEFAULTS.

Kinds table: kitlib/lock/kinds.json, written by the lock package and committed by the
  PM; this module only reads it. A repository's own kinds are its `.kit.json` "kinds" section
  (repo_kinds); kitlib.lock.kinds.repo_entry reads one, for a name the kit table does not have.

Repo hook module: `<root>/<tools_dir>/<module>.py`, named by `.kit.json` "hook".module.
  Loaded from the working tree only, never from a revision. Gate checks: the module's
  `GATE_CHECKS` is a dict of check name -> callable; a gate step `{"check": name}` runs one.
  Repo grammar (a Gradle pin check, dotnet result regexes, the py-drops list) lives there, with
  its settings in "hook".options. How a check is called and what it returns is gate.py's contract.
  It loads, and its checks run, with sys.dont_write_bytecode set (no_bytecode): no
  `__pycache__` is written into the repo's tree.
"""
import contextlib
import copy
import fnmatch
import hashlib
import importlib.util
import json
import ntpath
import os
import re
import sys

import kitlib.platform
from kitlib.content import names_of

from . import schema
from .types import ConfigError, check, is_note

DOTNET_BUILDS = ['build', 'test', 'publish', 'run', 'pack', 'msbuild', 'vstest']
GRADLE_INFO = ['--version', '-v', '--help', '-h', '-?', '--stop', '--status']

HEAVY_DEFAULTS = (
    {'program': 'godot', 'kind': 'app-run', 'when_args': [], 'unless_args': [],
     'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'godot.*', 'kind': 'app-run', 'when_args': [], 'unless_args': [],
     'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'godot_*', 'kind': 'app-run', 'when_args': [], 'unless_args': [],
     'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'blender*', 'kind': 'render', 'when_args': ['-b', '--background'], 'unless_args': [],
     'why': 'machine_lock classify.py render: a background Blender run'},
    # A .NET command that compiles, and any Gradle run but its information flags (dotnet --info and
    # gradle --version are not heavy).
    {'program': 'dotnet', 'kind': 'build', 'when_args': DOTNET_BUILDS, 'unless_args': [],
     'why': 'a .NET build, test, publish or run compiles the project'},
    {'program': 'dotnet.exe', 'kind': 'build', 'when_args': DOTNET_BUILDS, 'unless_args': [],
     'why': 'a .NET build, test, publish or run compiles the project'},
    {'program': 'gradle', 'kind': 'build', 'when_args': [], 'unless_args': GRADLE_INFO,
     'why': 'a Gradle run configures and builds the project'},
    {'program': 'gradle.*', 'kind': 'build', 'when_args': [], 'unless_args': GRADLE_INFO,
     'why': 'a Gradle run configures and builds the project'},
    {'program': 'gradlew', 'kind': 'build', 'when_args': [], 'unless_args': GRADLE_INFO,
     'why': 'a Gradle run configures and builds the project'},
    {'program': 'gradlew.*', 'kind': 'build', 'when_args': [], 'unless_args': GRADLE_INFO,
     'why': 'a Gradle run configures and builds the project'},
)

# The same programs as Linux names them (docs/linux-support-plan.md section 3, finding 8), added
# on Linux only: godot[0-9]* would make godot4.exe heavy on Windows too. godot4 and the like (an
# AppImage, Godot4.AppImage, too), flatpak's exported launchers (exports/bin/org.godotengine.Godot,
# exports/bin/org.blender.Blender), and `flatpak run <app id>`: flatpak's other verbs on an app id
# (install, info, remote-info) run nothing. `also_args` (kit defaults only, not a `.kit.json` key)
# is a list of argument sets, each needing one of its arguments present as well: the run verb,
# and for Blender the background flag.
GODOT_FLATPAKS = ['org.godotengine.Godot', 'org.godotengine.GodotSharp']
BLENDER_FLATPAKS = ['org.blender.Blender']
BLENDER_BACKGROUND = ['-b', '--background']

LINUX_HEAVY_DEFAULTS = (
    {'program': 'godot[0-9]*', 'kind': 'app-run', 'when_args': [], 'unless_args': [],
     'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'org.godotengine.godot*', 'kind': 'app-run', 'when_args': [], 'unless_args': [],
     'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'flatpak', 'kind': 'app-run', 'when_args': GODOT_FLATPAKS, 'also_args': [['run']],
     'unless_args': [], 'why': 'ARCHITECTURE.md Kinds: Godot runs are heavy'},
    {'program': 'org.blender.blender*', 'kind': 'render', 'when_args': BLENDER_BACKGROUND,
     'unless_args': [], 'why': 'machine_lock classify.py render: a background Blender run'},
    {'program': 'flatpak', 'kind': 'render', 'when_args': BLENDER_FLATPAKS,
     'also_args': [['run'], BLENDER_BACKGROUND], 'unless_args': [],
     'why': 'machine_lock classify.py render: a background Blender run'},
)

ROLE_DEFAULTS = {
    'main': {'edit_main_checkout': True, 'why': 'the session the owner or the PM launched'},
    'subagent': {'edit_main_checkout': False,
                 'why': 'ARCHITECTURE.md Guard rules: a subagent edit in a main checkout is denied'},
}

KINDS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lock', 'kinds.json')


# ---------------------------------------------------------------- heavy programs

def heavy_programs(cfg=None):
    """The kit defaults (and on Linux LINUX_HEAVY_DEFAULTS) followed by the repo's
    `heavy`.programs, each with every key present."""
    defaults = HEAVY_DEFAULTS + (LINUX_HEAVY_DEFAULTS if kitlib.platform.NAME == 'linux' else ())
    out = [copy.deepcopy(e) for e in defaults]
    for e in (cfg.section('heavy')['programs'] if cfg is not None else []):
        entry = {'when_args': [], 'unless_args': []}
        entry.update({k: copy.deepcopy(v) for k, v in e.items() if not is_note(k)})
        out.append(entry)
    return out


def _has(args, wanted):
    names = {a.split('=', 1)[0] if a.startswith('-') else a for a in args}
    return any(w in names for w in wanted)


def heavy_match(program, args, entries):
    """The first entry `program` (a path or a name) run with `args` matches, or None."""
    base = ntpath.basename(program.replace('/', '\\')).lower()
    for e in entries:
        if not fnmatch.fnmatchcase(base, e['program'].lower()):
            continue
        if e.get('when_args') and not _has(args, e['when_args']):
            continue
        if any(not _has(args, wanted) for wanted in e.get('also_args', ())):
            continue
        if e.get('unless_args') and _has(args, e['unless_args']):
            continue
        return e
    return None


# ---------------------------------------------------------------- roles

def roles(cfg=None):
    """ROLE_DEFAULTS with the repo's `roles` entries laid over (an entry's missing keys are taken
    from its base: `main` for `main`, else `subagent`)."""
    out = copy.deepcopy(ROLE_DEFAULTS)
    for name, entry in (cfg.section('roles') if cfg is not None else {}).items():
        base = copy.deepcopy(out.get(name) or out['main' if name == 'main' else 'subagent'])
        base.update({k: v for k, v in entry.items() if not is_note(k) and v is not None})
        out[name] = base
    return out


def role_name(action, table=None):
    """`main` (no Action.agent), the Action's agent_role when `table` names it, or the name `table`
    gives the same role under its old or new name (kitlib.content.ALIASES: a roles key
    `opus-worker` holds the senior-worker agent), else `subagent`."""
    if not getattr(action, 'agent', None):
        return 'main'
    kind = action.agent_role
    if not kind or kind == 'main':
        return 'subagent'
    known = table or ROLE_DEFAULTS
    return next((n for n in names_of(kind) if n in known and n != 'main'), 'subagent')


def permissions(action, table=None):
    table = table or roles()
    return dict(table[role_name(action, table)])


# ---------------------------------------------------------------- kinds table

def kinds_table(path=None):
    """The kinds table as a dict (kind -> its entry), notes dropped. ConfigError when the file is
    missing or is not a JSON object of objects."""
    path = path or KINDS_FILE
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except FileNotFoundError:
        raise ConfigError('no kinds table at %s' % path)
    except (OSError, ValueError) as e:
        raise ConfigError('cannot read the kinds table %s: %s' % (path, e))
    if not isinstance(data, dict):
        raise ConfigError('the kinds table %s must hold a JSON object' % path)
    wrapped = isinstance(data.get('kinds'), dict)
    table = data['kinds'] if wrapped else data
    if wrapped and 'aliases' in table:
        raise ConfigError('the kinds table %s: aliases belongs at the top level, beside kinds, '
                          'not inside it' % path)
    out = {k: v for k, v in table.items() if not is_note(k) and k != 'aliases'}   # a reserved key
    bad = sorted(k for k, v in out.items() if not isinstance(v, dict))
    if bad:
        raise ConfigError('the kinds table %s: entries %s must be objects' % (path, ', '.join(bad)))
    return out


def repo_kinds(cfg=None):
    """{kind: entry} of the repository's `.kit.json` "kinds" section, notes dropped at both levels
    ({} with no cfg). Each entry is as written, checked by the schema; the lock takes one only for
    a name that is neither a kit kind nor a kit alias (kitlib.lock.kinds.with_repo)."""
    out = {}
    for name, entry in (cfg.section('kinds') if cfg is not None else {}).items():
        out[name] = {k: copy.deepcopy(v) for k, v in entry.items() if not is_note(k)}
    return out


def kinds_aliases(path=None):
    """{alias: canonical kind} from the `aliases` object of the kinds table ({"alias": {"to": kind,
    "why": text}}); {} when the file is missing or names none. ConfigError when the file cannot
    be read, an alias is not an object with a `to` kind and a `why`, an alias is itself a kind,
    or its `to` is not a kind (so no chain or cycle). `aliases` is a reserved key, never a kind,
    in the flat form of the table too."""
    path = path or KINDS_FILE
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ConfigError('cannot read the kinds table %s: %s' % (path, e))
    aliases = data.get('aliases') if isinstance(data, dict) else None
    if aliases is None:
        return {}
    if not isinstance(aliases, dict):
        raise ConfigError('the kinds table %s: aliases must be an object' % path)
    out = {}
    for name, a in aliases.items():
        if is_note(name):
            continue
        if not (isinstance(a, dict) and isinstance(a.get('to'), str) and a['to']
                and isinstance(a.get('why'), str) and a['why']):
            raise ConfigError('the kinds table %s: alias %s needs a "to" kind and a "why"'
                              % (path, name))
        out[name] = a['to']
    kinds = kinds_table(path)
    for name, to in sorted(out.items()):
        if name in kinds:
            raise ConfigError('the kinds table %s: alias %s is a kind of its own' % (path, name))
        if to not in kinds:
            raise ConfigError('the kinds table %s: alias %s names %s, which is not a kind'
                              % (path, name, to))
    return out


# ---------------------------------------------------------------- repo hook module

def hook_path(cfg):
    """`(absolute path of the repo hook module, its options)`, or None when none is named."""
    h = cfg.hook()
    if not h:
        return None
    return os.path.join(cfg.root, cfg.tools_dir, h['module'] + '.py'), h['options']


@contextlib.contextmanager
def no_bytecode():
    """sys.dont_write_bytecode set while the repo's code runs, put back afterwards. The hook
    module and the siblings it imports are files in the work tree being gated: a
    `__pycache__/*.pyc` written beside them is an untracked file in that tree, and a gate that
    sees the tree change writes no gate-OK record (kitlib/gate/runner.py). Used to load the
    module (exec_without_bytecode) and to run its GATE_CHECKS (runner._run_check), where a
    check may import a sibling lazily."""
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        yield
    finally:
        sys.dont_write_bytecode = saved


def exec_without_bytecode(spec, module):
    """`spec.loader.exec_module(module)` under no_bytecode()."""
    with no_bytecode():
        spec.loader.exec_module(module)


def load_hook_module(cfg):
    """`(module, options)` of the repo's one hook module, or None when `.kit.json` names none.
    ConfigError when it is named but missing or fails to import (a named hook never silently
    becomes no hook; a module that calls sys.exit while it loads is one that fails). The
    module is in sys.modules under its name while it runs and after, as an import would put it
    (dataclasses, pickle and typing look a class's module up there); a module that fails, or is
    interrupted, is taken out again."""
    found = hook_path(cfg)
    if not found:
        return None
    path, options = found
    if not os.path.isfile(path):
        raise ConfigError('.kit.json names the hook module %s, which does not exist' % path)
    name = 'kit_repo_hook_%s' % hashlib.sha256(os.path.normcase(path).encode('utf-8')).hexdigest()[:12]
    module = None
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        exec_without_bytecode(spec, module)
    except BaseException as e:    # the repo's code: any failure, SystemExit too, is a config
        # failure, named; no half-loaded module is left behind in sys.modules
        if module is not None and sys.modules.get(name) is module:
            del sys.modules[name]
        if not isinstance(e, (Exception, SystemExit)):
            raise                     # Ctrl+C stops the caller
        raise ConfigError('the hook module %s failed to load: %s: %s' % (
            path, type(e).__name__, e.code if isinstance(e, SystemExit) else e))
    return module, options


def gate_checks(module):
    """The validated `GATE_CHECKS` of a loaded hook module (name -> callable); {} when it has
    none. ConfigError when it is not a dict, a name is not a step-name word, or a value is not
    callable."""
    checks = getattr(module, 'GATE_CHECKS', None)
    if checks is None:
        return {}
    # A module loaded from a commit (kitlib.gate.hookrev) is named as `<rev10>:<rel>`.
    where = getattr(module, '__kit_where__', None) or getattr(module, '__file__', 'the hook module')
    if not isinstance(checks, dict):
        raise ConfigError('%s: GATE_CHECKS must be a dict of check name -> callable' % where)
    errs = []
    for name, fn in checks.items():
        if not isinstance(name, str) or not re.fullmatch(schema.NAME_RE, name):
            errs.append('GATE_CHECKS key %r is not a check name' % (name,))
        elif not callable(fn):
            errs.append('GATE_CHECKS[%r] is not callable' % name)
    if errs:
        raise ConfigError('%s: %s' % (where, '; '.join(errs)))
    return dict(checks)


def load_gate_checks(cfg):
    """`(checks, options)`: the hook module's gate checks and its options, ({}, {}) when no hook
    is named. ConfigError when a gate step names a check the module does not provide."""
    found = load_hook_module(cfg)
    module, options = found if found else (None, {})
    checks = gate_checks(module) if module is not None else {}
    wanted = [(s['name'], s['check']) for s in cfg.section('gate')['steps'] or [] if s.get('check')]
    missing = ['%s (step %s)' % (c, n) for n, c in wanted if c not in checks]
    if missing:
        raise ConfigError('the hook module provides no gate check %s' % ', '.join(missing))
    return checks, options


def check_heavy_entry(entry, where='heavy entry'):
    return check(schema.HEAVY_ENTRY, entry, where)
