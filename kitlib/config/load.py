"""Reading a repository's `.kit.json` (from the PM kit's tools/_kit.py) and checking it whole.

`load(root)` reads the working tree; `load_at(root, rev)` reads what revision `rev` holds (a
landing or a pre-push check reads the BASE branch's config, never the branch's own). Either way
every section is read and checked at once and every problem is named in one ConfigError: a config
nobody could read never silently becomes "no config", and a misspelt section or key never
silently means "the default". A key written twice in one object refuses, and a section is read
only from `.kit.json` or a file it names: no tools/*.json is read because it exists (large-tools
audit pattern 6).

There is no environment override of the config path or the root (no env flag counts).

The file is optional, and every key in it an override (docs/kit-json.md "With no file"). Three
defaults are not fixed values: unset, `repo.main_branch` and `repo.name` are read from the
repository's git files (kitlib.git.defaults: `<remote>/HEAD`, then main or master; the remote's
URL, then the folder), each when the `repo` section is first asked for, once per Config; and
the owner rules' email (owner_rules) from the machine config's `owner_email`
(kitlib.config.user), read the first time the rules need an identity. A machine config that
cannot be read gives no owner email: every owner-only change is then refused, never allowed.
"""
import copy
import os
import re

from kitlib.git import cmd as gitcmd
from kitlib.git import defaults as gitdefaults
from kitlib.git import refs as gitrefs

from . import owner as ownermod
from . import schema
from . import user as kuser
from .types import ConfigError, check, is_note, parse_json

CONFIG_NAME = '.kit.json'
FIXED_MAIN = schema.SECTIONS['repo']['main_branch'].default   # guarded_mains
KIT_SECTIONS = ('repo', 'owner', 'hook', 'heavy', 'roles')
PLACEHOLDER = re.compile(r'\{([A-Za-z_]\w*)\}')
PLACEHOLDERS = ('python', 'root', 'base', 'here', 'tools')    # gate.py's substitution mapping
# Step keys about running a subprocess; a hook check is called, not run.
CMD_ONLY = ('cwd', 'daily_cmd', 'full_cmd', 'merged_args', 'ok_exits', 'reuse')


def _parse(text, where):
    data = parse_json(text, where)
    if not isinstance(data, dict):
        raise ConfigError('%s must hold a JSON object' % where)
    return data


def _read_file(path):
    """Text of `path`, or None when it does not exist. Any other failure raises ConfigError."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            return f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ConfigError('cannot read %s: %s' % (path, e))


def _git_show(root, rev, rel):
    """Text of `rev:rel`, or None when the revision does not hold that path."""
    why = gitrefs.bad_rev(rev)
    if why:
        raise ConfigError(why)
    r = gitcmd.call(['show', '%s:%s' % (rev, rel)], root)
    if r.rc == 0:
        return gitcmd.text(r.out).lstrip('﻿')
    err = (r.err or '').lower()
    if r.rc is not None and ('does not exist in' in err or 'exists on disk, but not in' in err):
        return None
    raise ConfigError('git show %s:%s failed: %s' % (rev, rel, r.err or 'exit %s' % r.rc))


def _gate_rules(gate, errs):
    """The gate's cross-field rules (PM kit gate.py validate_config): unique step names, known
    placeholders, contradictions. Built-in step names stay gate.py's to refuse.
    _build calls it only on a gate the schema passed; it still skips any value of the wrong type
    (the type check names it), so a wrongly typed gate is a ConfigError, never a TypeError."""
    seen = set()
    steps = gate.get('steps')
    for i, step in enumerate(steps if isinstance(steps, list) else ()):
        if not isinstance(step, dict):
            continue
        name = step.get('name')
        where = '"gate".steps[%d]' % i
        if isinstance(name, str):
            if name in seen:
                errs.append('%s: duplicate step name %r' % (where, name))
            seen.add(name)
        if step.get('always') and step.get('manual'):
            errs.append('%s: "always" and "manual" contradict each other' % where)
        if 'requires_strict' in step and 'requires' not in step:
            errs.append('%s: "requires_strict" needs "requires"' % where)
        if (step.get('cmd') is None) == (step.get('check') is None):
            errs.append('%s needs exactly one of "cmd" and "check"' % where)
        elif step.get('check') is not None:
            only = sorted(k for k in CMD_ONLY if k in step)
            if only:
                errs.append('%s: %s only apply to a "cmd" step' % (where, ', '.join(only)))
        if 'globs' not in step and not (step.get('always') or step.get('manual') or step.get('content')):
            errs.append('%s needs "globs" (or always, manual or content)' % where)
        requires = step.get('requires')
        requires = requires if isinstance(requires, list) else []
        argvs = [('cmd', step.get('cmd')), ('full_cmd', step.get('full_cmd')),
                 ('daily_cmd', step.get('daily_cmd')), ('merged_args', step.get('merged_args'))]
        argvs += [('requires', r) for r in requires if isinstance(r, list)]
        for key, argv in argvs:
            for arg in argv if isinstance(argv, list) else ():
                if not isinstance(arg, str):
                    continue
                unknown = sorted(set(PLACEHOLDER.findall(arg)) - set(PLACEHOLDERS))
                if unknown:
                    errs.append('%s "%s": unknown placeholder(s) %s' % (where, key, ', '.join(
                        '{%s}' % u for u in unknown)))
        for r in requires:
            if isinstance(r, str) and re.search(r'[\s/\\{]', r):
                errs.append('%s: "requires" %r must be a bare command name; a command with '
                            'arguments is a probe argv' % (where, r))


class Config(object):
    """A repository's checked kit configuration. `section(name)` is the section over its defaults
    (every schema key present, notes dropped); `raw(name)` is the section as written. `found` is
    False only when the reader answered that no `.kit.json` exists (a read that failed raised
    ConfigError instead), so it is the same read the sections came from. `home` is the home
    folder whose machine config gives the owner email when `owner.email` is unset (None: the
    user's); `machine`, when given, is a callable returning that machine config already read
    (the guard's Ctx.user, so a hook reads it once), used instead of reading it again."""

    def __init__(self, root, where, raw, files, found=True, home=None, machine=None):
        self.root = root
        self.where = where
        self.found = found
        self.home = home
        self._machine = machine
        self._raw = raw          # name -> dict as written (absent sections are {})
        self._files = files      # name -> repo-relative file it came from, or None (inline/absent)
        self._derived = {}       # repo.main_branch, repo.name, the machine owner email, each read once

    def raw(self, name):
        return copy.deepcopy(self._raw.get(name, {}))

    def has(self, name):
        """True when the section was written (inline or in a file `.kit.json` names)."""
        return name in self._files

    def section(self, name):
        if name not in schema.SECTIONS:
            raise KeyError('no such .kit.json section: %s' % name)
        raw = self._raw.get(name, {})
        if name in schema.OPAQUE or name in schema.MAPS:
            return {k: copy.deepcopy(v) for k, v in raw.items() if not is_note(k)}
        out = schema.defaults(name)
        out.update({k: copy.deepcopy(v) for k, v in raw.items() if not is_note(k) and v is not None})
        if name == 'repo':
            if raw.get('main_branch') is None:
                out['main_branch'] = self._derive('main_branch', lambda: gitdefaults.main_branch(
                    self.root, out['remote']))
            if not raw.get('name'):
                out['name'] = self._derive('name', lambda: gitdefaults.repo_name(self.root, out['remote']))
        return out

    def _derive(self, key, read):
        if key not in self._derived:
            self._derived[key] = read()
        return self._derived[key]

    def _machine_owner(self):
        """The machine config's owner_email, or None; None too when the machine config cannot
        be read (the owner rules then prove nothing: fail closed)."""
        def read():
            if self._machine is not None:
                m = self._machine() or {}
                if m.get('error'):              # Ctx.user's tolerant read of a broken config
                    return None
                value = m.get('owner_email')
                return value.strip().lower() if isinstance(value, str) and value.strip() else None
            try:
                return kuser.owner_email(self.home)
            except ConfigError:
                return None
        return self._derive('owner_email', read)

    def written(self, section, key):
        """The value `.kit.json` writes for `section`.`key`, or None when it writes none (a
        default, fixed or read from the repository, is not written)."""
        return copy.deepcopy(self._raw.get(section, {}).get(key))

    def section_file(self, name):
        """The repo-relative file a section lives in, or None when it is inline or absent."""
        return self._files.get(name)

    @property
    def repo(self):
        return self.section('repo')

    @property
    def main_branch(self):
        return self.repo['main_branch']

    def guarded_mains(self):
        """The branches the push rules (guard, pre-push) hold as main: main_branch, and, while
        `.kit.json` writes no main_branch, `main` too, the fixed default before it was read from
        the repository: `<remote>/HEAD` is local and any session can repoint it, so reading it
        never takes the protection off `main`."""
        main = self.main_branch
        if self.written('repo', 'main_branch') is None and main != FIXED_MAIN:
            return [main, FIXED_MAIN]
        return [main]

    @property
    def remote(self):
        return self.repo['remote']

    @property
    def push_block(self):
        return self.repo['push_block']

    @property
    def tools_dir(self):
        return self.repo['tools_dir']

    def owner(self):
        """The `owner` section over its defaults."""
        return self.section('owner')

    def owner_rules(self):
        """The owner rules; `owner.email` unset: the machine config's owner_email, read when
        the rules first need it."""
        owner = self.owner()
        return ownermod.from_section(owner, None if owner['email'] else self._machine_owner)

    def hook(self):
        """`{'module': name, 'options': {...}}` of the repo hook module, or None when no
        module is named. Options with no module are a ConfigError (load refuses them first)."""
        h = self._raw.get('hook', {})
        if not h.get('module'):
            if h.get('options'):
                raise ConfigError('%s: "hook".options needs a "hook".module' % self.where)
            return None
        return {'module': h['module'], 'options': copy.deepcopy(h.get('options') or {})}


def _build(root, where, reader, home=None, machine=None):
    text = reader(None)
    top = {} if text is None else _parse(text, where)
    errs = []
    bad = sorted(k for k in top if k not in schema.SECTIONS and not is_note(k)
                 and k not in schema.RETIRED_SECTIONS)
    if bad:
        errs.append('%s: unknown section(s) %s (known: %s)'
                    % (where, ', '.join(bad), ', '.join(sorted(schema.SECTIONS))))
    raw, files = {}, {}
    for name in schema.SECTIONS:
        value, rel = None, None
        if name in top:
            value = top[name]
            if isinstance(value, str) and name in schema.FILE_OK and value.strip():
                rel = value.strip().replace('\\', '/')
                if rel.startswith('/') or re.match(r'^[A-Za-z]:', rel) or '..' in rel.split('/'):
                    errs.append('%s: section "%s" names %s, which is not inside the repository'
                                % (where, name, rel))
                    continue
                body = reader(rel)
                if body is None:
                    errs.append('%s: section "%s" names %s, which does not exist' % (where, name, rel))
                    continue
                try:
                    value = _parse(body, rel)
                except ConfigError as e:
                    errs.append(str(e))
                    continue
            elif not isinstance(value, dict):
                errs.append('%s: section "%s" must be an object%s' % (
                    where, name, ' or a repo-relative .json path' if name in schema.FILE_OK else ''))
                continue
            files[name] = rel
        else:
            continue
        if isinstance(value, dict) and name in schema.RETIRED:
            value = {k: v for k, v in value.items() if k not in schema.RETIRED[name]}
        schema.section_type(name).check(value, '"%s"' % name, errs)
        raw[name] = value
    hook = raw.get('hook')
    if isinstance(hook, dict) and hook.get('options') and not hook.get('module'):
        errs.append('"hook".options needs a "hook".module')
    if isinstance(raw.get('gate'), dict) and not errs:     # cross-field rules walk checked types only
        _gate_rules(raw['gate'], errs)
        hooked = isinstance(hook, dict) and hook.get('module')
        checks = [s['name'] for s in raw['gate'].get('steps') or []
                  if isinstance(s, dict) and s.get('check') and isinstance(s.get('name'), str)]
        if checks and not hooked:
            errs.append('"gate": step(s) %s run a hook check, and no "hook".module is named'
                        % ', '.join(checks))
    if errs:
        raise ConfigError('%s: %s' % (where, '; '.join(errs)))
    return Config(root, where, raw, files, found=text is not None, home=home, machine=machine)


def config_path(root):
    return os.path.join(root, CONFIG_NAME)


def load(root, home=None, machine=None):
    """The working tree's checked Config (a repository with no `.kit.json` is all defaults).
    `home`: the home folder whose machine config gives an unset owner email (None: the user's);
    `machine`: that machine config already read (Config)."""
    path = config_path(root)
    return _build(root, path, lambda rel: _read_file(path if rel is None else os.path.join(root, rel)),
                  home, machine)


def load_at(root, rev, home=None):
    """The Config revision `rev` holds (`.kit.json` and its section files read with git show).
    The defaults read from the repository read `root`'s files as they are now."""
    return _build(root, '%s:%s' % (rev, CONFIG_NAME),
                  lambda rel: _git_show(root, rev, CONFIG_NAME if rel is None else rel), home)


def check_value(type_, value, where):
    """schema type check for a caller's own value (ConfigError naming every problem)."""
    return check(type_, value, where)
