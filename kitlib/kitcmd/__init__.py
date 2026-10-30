"""The `kit` command: `kit <tool> [arguments]` runs `<export>/tools/<tool>.py [arguments]` of the
plugin export the machine uses now (tools/kit.py is the entry point).

  resolve(here, settings)    (export folder, None) or (None, why): the folder the machine
                             config's plugin.export_dir holds and its marketplace names
  commands(kit)              the tool names `kit` runs from that folder
  check_name(name, have)     None, or why `name` is no tool `kit` runs
  argv(kit, name, args)      the child's command line
  run(kit, name, args)       start it in the caller's directory, wait, and give its exit code

Which kit: the machine config's `plugin.export_dir` (kitlib.pluginsource.load: the PM repo pointer,
then that repository's .pm-kit/config.json; unset, kitlib.pluginsource.default_export_dir:
%LOCALAPPDATA%\\pm-kit\\plugin, on Linux <profile>/.local/share/pm-kit/plugin, as for
tools/plugin_refresh.py), and in it the folder <export_dir>/.claude-plugin/marketplace.json names
(kitlib.pluginsource.current_folder), read at every call, so a refreshed export is used at the
next one. That folder must be an export (it holds kitlib.pluginsource.MARKER) with a tools folder.
A config that cannot be read, no marketplace, or a named folder that is missing or no export is a
refusal naming the fix; there is no fallback to the kit's main checkout or to the folder this code
is in. When this code itself runs from an export (`here` holds MARKER) that is not under that
export_dir (a `kit` left over from another export root), that is a refusal too.

How it runs: a child python (sys.executable) on `<export>/tools/<tool>.py` with the arguments as
given, in the caller's directory, with the caller's environment, through kitlib.proc.spawn. So -C
and every other argument reach the tool unchanged, the tool's default `-C .` is the caller's
directory, and the child's command line names the export's folder, which keeps a prune from
removing it while the tool runs (kitlib.pluginsource.read_in_use). The gate and land tools still
pin themselves to pushed main (kitlib.gate.pin).

Exit codes: the tool's own (all 32 bits, kitlib.proc.spawn.exit_code); REFUSED 2 for kit's own
usage error or refusal; INTERRUPTED 130 after Ctrl+C. The tool shares the console, so it gets the
same Ctrl+C: the first one waits for the tool to end (its own cleanup runs), a second ends it.

SHARED and KIT_ONLY partition the kit's tools: SHARED act on a repository checkout, KIT_ONLY on
the machine, the owner's files or the kit itself. `kit` runs either kind.
"""
import os
import re
import sys

from kitlib import pluginsource
from kitlib.config import ConfigError
from kitlib.config.user import HomeFenced
from kitlib.proc import spawn

REFUSED = 2
INTERRUPTED = 130
SELF = 'kit'                         # this command's own tool name, never run through itself

# The kit tools that act on a repository checkout named by -C or the current directory. A name
# the kit has no command for yet is left out.
SHARED = (
    'backlog', 'checkout_guard', 'codemap', 'commit', 'coord_state', 'daily_full', 'doc_budget',
    'doc_index', 'forbidden', 'gate', 'land', 'landed', 'ledger', 'machine_paths', 'module_map',
    'new_worktree',
    'pieces', 'preflight', 'pytests', 'redfirst', 'review_pack', 'stack_skill',
    'sync_base', 'testlist', 'tool_deps', 'trailers', 'verdict', 'worktree_paths',
    'worktree_remove',
)
# The kit tools that act on the machine, the owner's files or the kit itself, not on a repository
# checkout named by -C or the current directory. Every `tools/*.py` command but kit_stubs is in
# exactly one of SHARED and KIT_ONLY (tests/kitcmd: a new tool fails until it is classified).
KIT_ONLY = (
    'agent_roster', 'agent_temp_init', 'agent_view', 'auto_tasks', 'allow_rules', 'backlog_audit',
    'maint_lint', 'branch_overlap', 'config_move', 'delegated', 'hub', 'kit',
    'kit_tests', 'launch', 'lock_release_check', 'machine_lock', 'machine_setup', 'no_window',
    'pending', 'pm_data', 'pm_watch', 'plugin_refresh', 'render_content', 'worktree_sweep_task',
    'kit_release', 'stray_restore', 'ci_ratchet', 'node',
)

REFRESH_FIX = 'refresh the plugin export (tools/plugin_refresh.py --apply; docs/plugin-source.md)'
CONFIG_FIX = ('fix the machine config (tools/machine_setup.py --pm-repo sets the PM repo pointer; '
              'docs/machine-setup.md "PM repo pointer")')

_MAIN_GUARD = re.compile(r'''^if\s+__name__\s*==\s*['"]__main__['"]\s*:''', re.M)


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _under(path, top):
    p, t = _norm(path), _norm(top)
    return p == t or p.startswith(t.rstrip(os.sep) + os.sep)


def _is_export(folder):
    return os.path.isfile(os.path.join(folder, pluginsource.MARKER))


def settings():
    """The machine's plugin Settings (kitlib.pluginsource.load); raises what that raises."""
    return pluginsource.load()


def resolve(here=None, load=settings):
    """(export folder, None), or (None, why) naming the fix. `here` (default: the kit folder this
    code is in) is only checked when it is an export: it must lie under the configured export_dir.
    `load` gives the plugin Settings (a test seam)."""
    here = here or pluginsource.kit_root()
    try:
        s = load()
    except (ConfigError, pluginsource.RefreshError, OSError, ValueError, HomeFenced) as e:
        return None, 'the machine config cannot be read (%s: %s); %s' % (type(e).__name__, e,
                                                                       CONFIG_FIX)
    root = s.export_dir
    if _is_export(here) and not _under(here, root):
        return None, ('this kit command is in %s, which is not under the plugin export folder the '
                      'machine config names (plugin.export_dir %s); run the kit command from %s'
                      % (os.path.normpath(here), root, os.path.join(root, 'bin')))
    name = pluginsource.current_folder(root)
    if not name:
        return None, ('no plugin export is named: %s is missing or names no pm-kit folder; %s'
                      % (pluginsource.marketplace_path(root), REFRESH_FIX))
    folder = os.path.normpath(os.path.join(root, name))
    if os.path.dirname(folder) != os.path.normpath(root):
        return None, ('%s names %r, which is not a folder of %s; %s'
                      % (pluginsource.marketplace_path(root), name, root, REFRESH_FIX))
    if not os.path.isdir(os.path.join(folder, 'tools')) or not _is_export(folder):
        return None, ('the plugin export %s that %s names is missing or is not an export; %s'
                      % (folder, pluginsource.marketplace_path(root), REFRESH_FIX))
    return folder, None


def _read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def commands(kit):
    """The tool names in `kit`'s tools folder that `kit` runs: each `<name>.py` with a `__main__`
    block, but this command's own and a `_name` (a helper module), sorted."""
    tdir = os.path.join(kit, 'tools')
    try:
        names = sorted(os.listdir(tdir))
    except OSError:
        return []
    out = []
    for n in names:
        stem = n[:-3]
        if not n.endswith('.py') or not stem or stem.startswith('_') or stem == SELF:
            continue
        path = os.path.join(tdir, n)
        if os.path.isfile(path) and _MAIN_GUARD.search(_read(path) or ''):
            out.append(stem)
    return out


def check_name(name, have):
    """None when `name` is one of the tool names `have` (commands()), else why it is not."""
    if name == SELF:
        return 'kit does not run itself: name a tool, as in kit preflight -C <checkout>'
    if name.lower().endswith('.py') and name[:-3] in have:
        return 'name the tool without .py: kit %s' % name[:-3]
    if name not in have:
        return 'no kit tool named %r (kit with no arguments lists them)' % name
    return None


def argv(kit, name, args):
    """The child's command line: this python on `<kit>/tools/<name>.py`, then `args` unchanged."""
    return [sys.executable, os.path.join(kit, 'tools', name + '.py')] + list(args)


def wait(proc):
    """The exit code for sys.exit once `proc` (a Popen) ends: its own (spawn.exit_code), or
    INTERRUPTED after a Ctrl+C. The tool shares the console and gets the Ctrl+C too, so the first
    one waits for it to end; a second one ends it (Popen.kill) and waits."""
    interrupted = False
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            if interrupted:
                proc.kill()
            interrupted = True
    return INTERRUPTED if interrupted else spawn.exit_code(code)


def run(kit, name, args, popen=spawn.popen):
    """Start `<kit>/tools/<name>.py args` in the caller's directory and give wait()'s exit code.
    OSError when python cannot be started. `popen` is a test seam."""
    return wait(popen(argv(kit, name, args)))
