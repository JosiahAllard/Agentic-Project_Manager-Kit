"""The machine-wide managed settings file of Claude Code (docs/machine-setup.md "Managed settings"):
`%ProgramFiles%\\ClaudeCode\\managed-settings.json`, which every account on the machine loads. The
shared settings live there, so an account's own settings.json holds nothing of the kit's.

The file's path is the default host's (its adapter's optional managed_settings_path(), read by
extras.Host.managed_path; a host without it has no managed file, and every step here is skipped).

survey() is the --managed-settings mode: it renders the kit's template, source(), and plans the one
write that makes the managed file hold it. The file is under Program Files (on Linux,
/etc/claude-code/managed-settings.json), so --apply is refused unless the process is elevated
(an administrator terminal; on Linux root or sudo); nothing elevates itself. The default mode never writes it:
note_sync() notes whether it is in sync and names the command.

Rendering: the template is a JSON object. The kit is stable_kit(ctx.kit): a plugin export, which
every plugin refresh replaces, stands for the main checkout its marker records, so the template is
read there and the guard hook and status line name a path that outlives the refresh. In every
string, {kit} becomes that folder (forward slashes), {pythonw} the guard entry's interpreter (extras.Host.guard_entry) and {plugin_export} the
plugin export root (kitlib.pluginsource). Then the overlay, OVERLAY in the machine config folder
(`<PM repo>/.pm-kit/managed-settings.json`, kitlib.config.user.config_dir; absent by default), is merged over it: objects key by key,
anything else replaced. The overlay holds what the committed template must not: the agents' git
identity (env GIT_*), and the auto-mode lines that name the owner, accounts or home paths. Last, a
hook or statusLine path through `<config>/tools/<link>` is re-pointed to the kit's own file
(settings.repoint_links), as in the account settings. Off Windows the WINDOWS_ONLY keys are dropped
from the template before all that, and a hook running this kit's guard with no onFailure gets the
guard entry's (kitlib.host.hookmain.ON_FAILURE, through boot/hook.py --settings-entry;
settings.add_on_failure): on Linux the managed guard blocks the call when its interpreter is gone,
as the account entry it replaces would. On Linux {pythonw} is the interpreter that ran machine
setup, by its full path (hookmain.command_line).

The written file keeps the layout the managed file has (BOM, line ends, indent); a new one is
written in settings.NEW_LAYOUT. An existing file is replaced whole, so the run is refused when it
is not a JSON object or has a key at any depth the rendered template lacks (named by its dotted
path: add it to the template; values inside lists are not walked). The write keeps the key order
the existing file has, so the diff shows only real changes. A replaced file is copied to a .bak
beside it first, and the write is refused when the file changed since the survey or is a link.
A key the template dropped on purpose is listed in RETIRED: the replace may drop it, and the plan
line and the diff name it.

survey_live() reads the managed file as it is, for the default mode and the older-kit switch: when
it carries this kit's guard entry, no account settings.json gains one (settings). The plugin step
does not read it: each account still gets the migrate that records it in the export's state.
"""
import copy
import difflib
import json
import os
import re

from kitlib import platform
from kitlib.config import user

from .common import Plan, free_name, is_link, kit_file, norm, python_name
from . import settings

SOURCE = 'hosts/%s/managed-settings.json'                         # the default host's (source())
OVERLAY = 'managed-settings.json'
# Template keys rendered on Windows only: the status line runs powershell and a .ps1. The overlay,
# merged after, may still give another platform its own.
WINDOWS_ONLY = ('statusLine',)
SECTION = 'managed'
PLACEHOLDER = re.compile(r'\{(kit|pythonw|plugin_export)\}')
COMMAND = 'tools/machine_setup.py'
# Dotted paths the template once held and dropped: a managed file that still has one may lose it in
# the replace (named in the diff and the plan line); any other key the template and overlay lack
# still refuses. Add a path here in the same commit that drops it from the template.
RETIRED = (
    'effortLevel',      # dropped by the commit "Managed settings: drop effortLevel and modelSettings"
    'modelSettings',    # the same commit (it held claude-opus-5-5.autoCompactWindow)
)


class ManagedError(Exception):
    """The template, the overlay or a placeholder's value cannot be read."""


def source():
    """The template's kit-relative path: SOURCE in hosts.json's default host's folder."""
    from kitlib.host import registry
    return SOURCE % registry.default()


def command(kit, apply=True):
    return '%s %s --managed-settings%s' % (python_name(), kit_file(kit, COMMAND).replace('\\', '/'),
                                           ' --apply' if apply else '')


def _read_object(path, what):
    try:
        with open(path, 'rb') as f:
            data = json.loads(f.read().decode('utf-8-sig'))
    except (OSError, ValueError) as e:
        raise ManagedError('%s %s cannot be read as JSON: %s' % (what, path, e))
    if not isinstance(data, dict):
        raise ManagedError('%s %s does not hold a JSON object' % (what, path))
    return data


def overlay_path(home):
    """The overlay's path in the PM repository's machine config folder; None when there is none
    (no good PM-repo pointer, or a machine config that cannot be read): the home folder holds no
    overlay any more."""
    folder = user.pm_config_dir(home)
    return os.path.join(folder, OVERLAY) if folder else None


def overlay(home):
    """The overlay object; {} when there is no file."""
    path = overlay_path(home)
    if not path or not os.path.lexists(path):
        return {}
    data = _read_object(path, 'the managed settings overlay')
    env = data.get('env')
    if env is not None and not (isinstance(env, dict)
                                and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
        raise ManagedError('the managed settings overlay %s: "env" must be an object of strings' % path)
    return data


def merge(base, over):
    """`over` merged into a copy of `base`: objects key by key, anything else replaced."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def stable_kit(kit):
    """The kit folder the managed file names for `kit`: `kit` itself, or, for a plugin export
    (kitlib.pluginsource MARKER), the main checkout its marker records (pluginsource.default_kit),
    since every plugin refresh replaces the export folder. ManagedError for an export whose
    checkout has moved or gone."""
    from kitlib import pluginsource as ps
    got, why = ps.default_kit(kit)
    if got is None:
        raise ManagedError('%s (a plugin export is replaced at every plugin refresh, so the managed '
                           'settings name the kit checkout)' % why)
    return got


def _same_kit(root):
    """stable_kit(root), or `root` itself when it cannot be resolved (guard_where only compares)."""
    try:
        return stable_kit(root)
    except ManagedError:
        return root


def _guard_hook(ctx):
    """The guard entry's hook (extras.Host.guard_entry): its command is {pythonw}, and its
    onFailure, when it has one, the managed guard hook's too (render)."""
    entry = ctx.host.guard_entry(ctx.kit)
    for g in entry.get('PreToolUse') or []:
        for h in g.get('hooks') or []:
            if isinstance(h, dict) and isinstance(h.get('command'), str):
                return h
    raise ValueError('boot/hook.py --settings-entry names no interpreter')


def _plugin_export(ctx):
    from kitlib import pluginsource as ps
    return ps.load(ctx.home, kit=ctx.kit).export_dir


def _substitute(data, values):
    if isinstance(data, str):
        return PLACEHOLDER.sub(lambda m: values(m.group(1)), data)
    if isinstance(data, list):
        return [_substitute(x, values) for x in data]
    if isinstance(data, dict):
        return dict((k, _substitute(v, values)) for k, v in data.items())
    return data


def render(ctx):
    """The managed settings the kit's template makes on this machine; None when the kit has no
    template. ManagedError when it cannot be made."""
    try:
        rel = source()
    except (OSError, ValueError) as e:                               # registry.RegistryError
        raise ManagedError('cannot name the template: %s' % e)
    kit = stable_kit(ctx.kit)
    path = kit_file(kit, rel)
    if not os.path.lexists(path):
        return None
    data = _read_object(path, 'the template')
    if platform.NAME != 'windows':
        for k in WINDOWS_ONLY:
            data.pop(k, None)
    got = {}

    def values(name):
        if name not in got:
            try:
                if name == 'kit':
                    got[name] = kit.replace('\\', '/')
                elif name == 'pythonw':
                    got[name] = values('hook')['command']
                elif name == 'hook':
                    got[name] = _guard_hook(ctx)
                else:
                    got[name] = _plugin_export(ctx)
            except ManagedError:
                raise
            except Exception as e:                                   # a config, CLI or plugin error
                raise ManagedError('cannot fill {%s}: %s' % ('pythonw' if name == 'hook' else name, e))
        return got[name]
    data = merge(_substitute(data, values), overlay(ctx.home))
    here = ctx
    if kit != ctx.kit:
        here = copy.copy(ctx)                                        # links re-pointed into the checkout
        here.kit = kit
    try:
        settings.repoint_links(data, here)
    except ValueError as e:
        raise ManagedError(str(e))
    settings.add_on_failure(data, kit, ctx.home, lambda: values('hook').get('onFailure'))
    return data


def guard_where(data, kit, home):
    """'kit' when `data` has a PreToolUse hook running this kit's boot/hook.py; else the paths of
    another kit's boot/hook.py it runs (a list, maybe empty). A plugin export and the checkout its
    marker records are one kit (stable_kit): a file rendered from either is this kit's."""
    if not isinstance(data, dict):
        return []
    guard = norm(kit_file(_same_kit(kit), settings.GUARD))
    tail = os.sep + os.path.normcase(os.path.join('boot', 'hook.py'))
    others = []
    for p in settings._hook_paths(data, 'PreToolUse', home):
        if norm(p) == guard:
            return 'kit'
        if norm(p).endswith(tail) and norm(kit_file(_same_kit(os.path.dirname(os.path.dirname(p))),
                                                    settings.GUARD)) == guard:
            return 'kit'
        if norm(p).endswith(tail) and p not in others:
            others.append(p)
    return others


def _target(plan, path, data):
    """(raw, layout, parsed, retired) of the managed file at `path`; (None, NEW_LAYOUT, None, [])
    when there is none; None after a refusal. An existing file is replaced whole, so it is refused
    when it is no JSON object or holds a key at any depth that `data` (the rendered template,
    overlay included) lacks (_extra_paths), except a RETIRED one: those are returned as `retired`."""
    if not os.path.lexists(path):
        return None, dict(settings.NEW_LAYOUT), None, []
    if is_link(path) or not os.path.isfile(path):
        plan.refuse('%s is a link or not a file; this step replaces only a plain file' % path)
        return None
    shared = settings.shared_link(path)
    if shared:
        plan.refuse('%s is %s; replacing it would split it from the file it shares' % (path, shared))
        return None
    with open(path, 'rb') as f:
        raw = f.read()
    try:
        layout, have = settings.read(raw)
    except ValueError as e:
        plan.refuse('%s is not JSON this step can edit: %s' % (path, e))
        return None
    if not isinstance(have, dict):
        plan.refuse('%s does not hold a JSON object' % path)
        return None
    extra = _extra_paths(have, data)
    retired = [k for k in extra if k in RETIRED]
    extra = [k for k in extra if k not in RETIRED]
    if extra:
        plan.refuse('%s has keys that are neither in the kit template nor in the overlay, and replacing '
                    'the file would drop them: %s; add them to the template (%s)'
                    % (path, ', '.join(extra), source()))
        return None
    return raw, layout, have, retired


def _extra_paths(have, data, prefix=''):
    """The dotted paths of the keys `have` holds that `data` lacks, sorted. Objects are walked key
    by key, as merge() does; an object where `data` has a non-object is lost whole and named at
    its own path. Lists are replaced values and are not walked."""
    out = []
    for k, v in have.items():
        here = prefix + k
        if k not in data:
            out.append(here)
        elif isinstance(v, dict):
            if isinstance(data[k], dict):
                out.extend(_extra_paths(v, data[k], here + '.'))
            else:
                out.append(here)
    return sorted(out)


def _ordered_like(data, have):
    """A copy of `data` whose objects list their keys in the order `have` does, keys `have` lacks
    last in `data`'s order: the written file moves nothing the old one had, so a diff shows real
    changes only."""
    if not isinstance(data, dict):
        return data
    old = have if isinstance(have, dict) else {}
    out = dict((k, _ordered_like(data[k], old[k])) for k in old if k in data)
    for k in data:
        if k not in out:
            out[k] = data[k]
    return out


class ManagedContext(object):
    """What the --managed-settings mode reads: the home (the machine config's overlay), the kit
    (its template), `host` (extras.Host: the managed file's path, elevation, the guard entry)."""

    def __init__(self, home, kit, host=None, today=None):
        import datetime
        from . import extras
        self.home = os.path.abspath(home)
        self.kit = os.path.abspath(kit)
        self.config_dirs = []                                        # repoint_links adds the home's
        self.host = host or extras.Host()
        self.today = today or datetime.date.today().strftime('%Y%m%d')


def survey(ctx, apply=False):
    """The --managed-settings plan: one write of the rendered template (in the existing file's key
    order), or nothing."""
    plan = Plan((SECTION,))
    try:
        kit = stable_kit(ctx.kit)                                    # the folder the hook will name
    except ManagedError as e:
        plan.refuse(str(e))
        return plan
    if not os.path.isfile(kit_file(kit, settings.GUARD)):
        plan.refuse('%s is not a kit checkout (no %s)' % (kit, settings.GUARD))
        return plan
    path = ctx.host.managed_path()
    if not path:
        plan.note(SECTION, 'the default host has no machine-wide managed settings file: nothing to install')
        return plan
    try:
        data = render(ctx)
    except ManagedError as e:
        plan.refuse(str(e))
        return plan
    if data is None:
        plan.note(SECTION, 'no %s in the kit: nothing to install' % source())
        return plan
    if guard_where(data, ctx.kit, ctx.home) != 'kit':
        plan.note(SECTION, 'the template carries no guard entry for this kit (%s): each account '
                           'settings.json keeps its own' % kit_file(kit, settings.GUARD))
    if not ctx.host.is_admin():
        if apply:
            if platform.NAME == 'windows':
                plan.refuse('not elevated: %s is under Program Files; run %s from an administrator '
                            'terminal' % (path, command(kit)))
            else:
                plan.refuse('not elevated: %s is writable only by root; run %s as root or with sudo'
                            % (path, command(kit)))
            return plan
        plan.note(SECTION, 'not elevated: --apply is refused here; run it %s' % _elevated_where())
    got = _target(plan, path, data)
    if got is None:
        return plan
    raw, layout, have, retired = got
    new = settings.write(layout, _ordered_like(data, have))
    if new == raw:
        plan.note(SECTION, '%s is in sync with the kit template' % path)
        return plan
    old = raw.decode('utf-8-sig', 'replace').replace('\r\n', '\n') if raw is not None else ''
    diff = [l.rstrip('\r\n') for l in difflib.unified_diff(
        old.splitlines(True), new.decode('utf-8-sig').replace('\r\n', '\n').splitlines(True),
        path if raw is not None else path + ' (none)', path + ' (after)', n=1)]
    if raw is None:
        plan.add(SECTION, '%s: create it from %s' % (path, source()), lambda: _create(path, new),
                 undo='delete "%s"' % path, detail=diff)
        return plan
    bak = free_name('%s.machine-setup-%s.bak' % (path, ctx.today))
    dropped = ' and drops the retired key(s) %s' % ', '.join(retired) if retired else ''
    plan.add(SECTION, '%s: replace it with the rendered %s%s (backup: %s)'
             % (path, source(), dropped, os.path.basename(bak)),
             lambda: settings._write(path, raw, new, bak), undo='copy "%s" over "%s"' % (bak, path),
             detail=diff)
    return plan


def _elevated_where():
    return 'from an administrator terminal' if platform.NAME == 'windows' else 'as root or with sudo'


def _create(path, new):
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    settings._create(path, new)


def note_sync(plan, ctx, section='settings'):
    """The default mode's note: is the managed file what --managed-settings would write?"""
    path = ctx.host.managed_path()
    if not path:
        return
    try:
        data = render(ctx)
    except ManagedError as e:
        plan.note(section, 'managed settings: cannot render the kit template: %s' % e)
        return
    if data is None:
        plan.note(section, 'managed settings: no %s in the kit; %s is not checked' % (source(), path))
        return
    raw = None
    if os.path.isfile(path):
        with open(path, 'rb') as f:
            raw = f.read()
    try:
        same = raw is not None and settings.read(raw)[1] == data
    except ValueError:
        same = False
    if same:
        plan.note(section, 'managed settings: %s is in sync with the kit template' % path)
    else:
        plan.note(section, 'managed settings: %s %s the kit template; %s: %s (a dry run first without --apply)'
                  % (path, 'differs from' if raw is not None else 'is missing; it is made from',
                     _elevated_where(), command(_same_kit(ctx.kit))))


def survey_live(plan, ctx):
    """Read the managed file as it is into ctx.managed_data (None: no file) and note what it means
    for the account settings; refused when it is there but no JSON object."""
    path = ctx.host.managed_path()
    ctx.managed_file, ctx.managed_data = path, None
    if not path or not os.path.lexists(path):
        return
    try:
        ctx.managed_data = _read_object(path, 'the managed settings file')
    except ManagedError:
        plan.refuse('%s cannot be read as a JSON object, so machine setup cannot tell whether it '
                    'carries the guard' % path)
        return
    where = guard_where(ctx.managed_data, ctx.kit, ctx.home)
    if where == 'kit':
        plan.note('settings', 'the guard\'s entry is in the managed settings file %s: no account '
                              'settings.json gains one' % path)
    elif where:
        plan.note('settings', 'the managed settings file %s names another kit\'s guard (%s): no '
                              'account settings.json gains this kit\'s; %s re-renders it'
                  % (path, ', '.join(where), command(ctx.kit, apply=False)))
