"""The plugin's source: an export of pushed kit main that every account folder's Claude Code
installs from (docs/plugin-source.md).

  default_export_dir()             the export root with no plugin.export_dir: %LOCALAPPDATA%\\pm-kit\\
                                   plugin, on Linux <profile>/.local/share/pm-kit/plugin
  remote_main(kit, remote)         the sha the remote's main points at; refuses one not fetched
  export(kit, sha, root)           <root>/<sha12>/: the tracked files at sha but tests/, plugin.json
                                   stamped, each per-platform manifest the running platform's
                                   (platform_manifests),
                                   the marker naming the kit's main checkout
  export_checkout(folder)          that main checkout, while it still is one, or None
  write_marketplace(root, folder)  <root>/.claude-plugin/marketplace.json names that folder
  read_in_use(root)                what running processes, scheduled tasks and pin leases name
  prune(root, in_use=)             old export folders removed by claim (never the named one, the 3
                                   newest, one in use, or one replaced less than 7 days ago, 1 when
                                   every in-use read succeeded); stale <sha12>.tmp folders removed
  prune_caches(folders, in_use)    old versions in each account's plugins cache (dry run by default)
  read_state / write_state         <root>/state.json: {sha, version, folder, exported_at, folders}
  RefreshLock                      <root>/refresh.pid, one refresh at a time
  pick_folders(folders, wanted)    the account folders --folder names, and the names that are not one
  refresh(...)                     the whole run tools/plugin_refresh.py makes (with copies, each
                                   host's own plugin copy too: tools/_host.py plugin_copies)
  installs(folders)                [(install path, account folder)] of each account's kit install
  marketplace_mismatches(export_dir, folders, managed)
                                   lines where a marketplace path is not the export (read only)
  current_lines(kit, s, folders)   the pm_start warning lines (no export, not migrated, behind, failed, marketplace off the export)
  poll(kit, s, last, ...)          the pm_watch check: an event line per new main, and the spawn
                                   of a refresh when plugin.auto_refresh is on

The export holds tracked files only (git ls-tree of the commit, blobs from one cat-file batch),
so no worktree, no .git and nothing untracked reaches a plugin cache. It reads git and writes
only under the export root (and, for prune_caches with apply or a refresh that went through,
under each account's plugins cache; a host's plugin-copy step, which refresh is handed, writes
that host's own copy);
it fetches nothing and takes no machine lock.
"""
import collections
import datetime
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import time

from kitlib import kitroot
from kitlib import platform as kitplatform
from kitlib.git import batch, cmd, refs, repo

NAME = 'pm-kit'                      # the marketplace's and the plugin's name
PLUGIN_ID = 'pm-kit@pm-kit'
MAIN = 'main'
DEFAULT_REMOTE = 'origin'
MARKER = '.pm-kit-export'            # in every export folder: this tool made it (prune reads it)
STATE = 'state.json'
PIDFILE = 'refresh.pid'
LOGFILE = 'refresh.log'
KEEP = 3                             # newest export folders prune keeps, besides the named one
YOUNG_DAYS = 7                       # prune keeps every export replaced less than this many days ago
IN_USE_YOUNG_DAYS = 1                # ... cut to this when every in-use read succeeded (read_in_use)
CLAIM = '.pruning-'                  # a folder prune is removing: <name>.pruning-<pid>
LIST_TIMEOUT = 30                    # seconds for the process list and the task list
LOCK_GRACE_S = 10                    # an empty or unreadable refresh.pid younger than this is live
MANIFEST = '.claude-plugin/plugin.json'
HOOKS_FILE = 'hooks/hooks.json'
# The manifests a renderer writes once per platform, `<name>.<platform>.json` beside each
# (platform_manifests): the hooks, Codex's MCP servers and Claude's plugin manifest.
PLATFORM_FILES = (HOOKS_FILE, 'mcp.json', MANIFEST)
MARKETPLACE = '.claude-plugin/marketplace.json'
CONFIG_KEY = 'plugin'
LEFT_OUT = 'tests'                   # the top-level folder export leaves out (kitrelease does too)
_SHA12 = re.compile(r'^[0-9a-f]{12}$')
_TMP = re.compile(r'^([0-9a-f]{12})\.tmp$')
_CLAIMED = re.compile(r'^.+\.pruning-\d+$')
_ENDS = r'(?=[\\/"\'\s<;,)&]|$)'      # what may follow a named path part: a separator, a quote,
                                       # a PYTHONPATH-style list, a closing paren, a cmd && chain
_NAMED = re.compile(r'(?:^|(?<=[\\/"\'\s=>]))([0-9a-f]{12})' + _ENDS)
_RESERVED = frozenset(['CON', 'PRN', 'AUX', 'NUL'] + ['COM%d' % i for i in range(1, 10)]
                      + ['LPT%d' % i for i in range(1, 10)])
_VERSION = re.compile(rb'("version"\s*:\s*")([^"\\]*)(")')
_BOM = b'\xef\xbb\xbf'
_NOWIN = 0x08000000 if sys.platform == 'win32' else 0               # CREATE_NO_WINDOW
_DETACHED = 0x00000008 | 0x00000200 if sys.platform == 'win32' else 0  # DETACHED_PROCESS, NEW_GROUP


class RefreshError(Exception):
    """A refusal: the run stops, with this reason, and changes nothing more."""


class StateError(Exception):
    """state.json exists but cannot be read as a state."""


# ---------------------------------------------------------------- settings

Settings = collections.namedtuple('Settings', 'export_dir auto_refresh remote')


def _norm(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _under(path, top):
    """True when `path` is `top` or inside it."""
    p, t = _norm(path), _norm(top)
    return p == t or p.startswith(t.rstrip(os.sep) + os.sep)


def placement_errors(root, kit=None, sweep_dir=None):
    """Errors for an export root inside the kit checkout (the export would then be part of the
    tree it exports, and a worktree clean could take it) or under the agent temp sweep root
    (agent_temp_sweep.dir, whose sweep removes unused entries)."""
    errs = []
    if kit and _under(root, kit):
        errs.append('plugin.export_dir %s is inside the kit checkout %s: put it outside' % (root, kit))
    if isinstance(sweep_dir, str) and sweep_dir.strip() and os.path.isabs(sweep_dir) \
            and _under(root, sweep_dir):
        errs.append('plugin.export_dir %s is under the agent temp sweep root %s (agent_temp_sweep.dir), '
                    'which removes unused entries: put it elsewhere' % (root, sweep_dir))
    return errs


def check(obj, kit=None, sweep_dir=None):
    """Errors in the machine config's `plugin` object (kitlib.config.user registers the key as an
    object; its inner keys are checked here). With `kit` or `sweep_dir`, an export_dir inside
    either is an error too (placement_errors)."""
    errs = []
    if obj is None:
        return errs
    if not isinstance(obj, dict):
        return ['plugin must be an object']
    for k in sorted(obj):
        if k.startswith('_'):
            continue
        v = obj[k]
        if k == 'export_dir':
            if not isinstance(v, str) or not v.strip() or not os.path.isabs(v):
                errs.append('plugin.export_dir must be an absolute path, not %r' % (v,))
            else:
                errs.extend(placement_errors(v, kit, sweep_dir))
        elif k == 'auto_refresh':
            if not isinstance(v, bool):
                errs.append('plugin.auto_refresh must be true or false, not %r' % (v,))
        elif k == 'remote':
            if not isinstance(v, str) or refs.bad_rev(v) or not v.strip():
                errs.append('plugin.remote must be a git remote name, not %r' % (v,))
        else:
            errs.append('plugin.%s is not a known key (export_dir, auto_refresh, remote)' % k)
    return errs


LINUX_EXPORT = ('.local', 'share', 'pm-kit', 'plugin')    # under the Linux profile folder


def default_export_dir(environ=None):
    """The export root when the machine config sets no plugin.export_dir, the one default every
    reader (settings, so kitlib.gate.pin, kitlib.kitcmd and the installers) gets. Windows (and
    any platform but Linux): %LOCALAPPDATA%\\pm-kit\\plugin, LOCALAPPDATA from `environ`. Linux:
    <profile>/.local/share/pm-kit/plugin, the profile from the password database
    (kitlib.platform.linux.paths.profile), never $HOME or $XDG_DATA_HOME: a cron job, a systemd
    unit and a login shell of the user all get the folder the managed settings name, as they all
    get the same PM-repo pointer. RefreshError when it cannot be told, a Python with no password
    database module (no pwd: Linux patched in on Windows) included, so a caller that skips the
    export on RefreshError (kitlib.guard.rules.advice_kit) skips it there too."""
    if kitplatform.NAME == 'linux':
        from kitlib.platform.linux import paths as linux_paths
        try:
            profile = linux_paths.profile()
        except (OSError, ImportError) as e:
            raise RefreshError('the profile folder cannot be read (%s): set plugin.export_dir in '
                               'the machine config' % e)
        return posixpath.join(profile, *LINUX_EXPORT)
    env = os.environ if environ is None else environ
    base = env.get('LOCALAPPDATA')
    if not base or not os.path.isabs(base):
        raise RefreshError('LOCALAPPDATA is not set: set plugin.export_dir in the machine config')
    return os.path.join(base, 'pm-kit', 'plugin')


def settings(obj, environ=None, kit=None, sweep_dir=None):
    """The checked Settings of a `plugin` object (None: every default). The default export_dir is
    default_export_dir's; RefreshError when that is needed and cannot be told, and when the
    export root is inside `kit` or `sweep_dir` (placement_errors)."""
    errs = check(obj)
    if errs:
        raise RefreshError('; '.join(errs))
    obj = obj or {}
    root = obj.get('export_dir') or default_export_dir(environ)
    errs = placement_errors(root, kit, sweep_dir)
    if errs:
        raise RefreshError('; '.join(errs))
    return Settings(os.path.normpath(root), obj.get('auto_refresh') is True,
                    obj.get('remote') or DEFAULT_REMOTE)


def load(home=None, environ=None, kit=None):
    """The machine's plugin Settings (kitlib.config ConfigError for a bad machine config). The
    export root must lie outside `kit` (default: this checkout) and agent_temp_sweep.dir."""
    from kitlib.config import user
    cfg = user.user_config(home)
    sweep = cfg.get('agent_temp_sweep')
    return settings(cfg.get(CONFIG_KEY), environ, kit or kit_root(),
                    sweep.get('dir') if isinstance(sweep, dict) else None)


def kit_root():
    """The checkout this module is in."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


LIVE_ROOT_ENV = 'KIT_LIVE_ROOT'      # set by kitlib.gate.pin for a tool it runs from an export


def live_root(kit=None, environ=None):
    """The kit checkout a printed command names. A checkout (`kit`, default: this one, holding no
    MARKER) names itself. An export (it holds MARKER) names KIT_LIVE_ROOT when the variable names
    a folder, else the main checkout its marker records (export_checkout) when that holds kit.json
    and a tools folder (the test kitlib.stubs.live_kit makes), else itself. A tool pinned to an
    export of pushed main (kitlib.gate.pin), or run from the plugin's export with no KIT_LIVE_ROOT
    (the session start hook, pm_start), runs from a folder a later prune may remove."""
    kit = kit or kit_root()
    if not os.path.isfile(os.path.join(kit, MARKER)):
        return kit
    env = os.environ if environ is None else environ
    live = env.get(LIVE_ROOT_ENV)
    if live and os.path.isdir(live):
        return live
    checkout = export_checkout(kit)
    if checkout and os.path.isfile(os.path.join(checkout, 'kit.json')) \
            and os.path.isdir(os.path.join(checkout, 'tools')):
        return os.path.normpath(checkout)
    return kit


def refresh_cmd(migrate=False):
    """`python "<kit>/tools/plugin_refresh.py" [--migrate] --apply`, the kit (live_root) named in
    full: the line reaches a PM whose session runs in another folder, and no repository holds a
    copy of the kit's tools (decision 21 in docs/multi-host-plan.md section 7)."""
    return '%s%s --apply' % (kitroot.command('plugin_refresh', kit=live_root()),
                             ' --migrate' if migrate else '')


# ---------------------------------------------------------------- git side

def remote_main(kit, remote):
    """The full sha `remote`'s main points at (ls-remote: read-only, writes no ref). RefreshError
    when the remote cannot be read, has no main, or the commit is not in the kit's objects."""
    sha, why = refs.ls_remote(remote, 'refs/heads/%s' % MAIN, kit)
    if sha is None:
        raise RefreshError('cannot read %s/%s: %s' % (remote, MAIN, why))
    if not sha:
        raise RefreshError('%s has no %s branch' % (remote, MAIN))
    found, why = batch.commits([sha], kit)
    if found is None:
        raise RefreshError('cannot look for %s in the kit: %s' % (sha[:12], why))
    if not found.get(sha):
        raise RefreshError('%s/%s is %s, which the kit checkout does not have: fetch the kit first '
                           '(git -C "%s" fetch %s)' % (remote, MAIN, sha[:12], kit, remote))
    return sha


def source_kit(kit):
    """The checkout git runs against for `kit`: `kit` itself, or, when `kit` is an export (it holds
    MARKER; the plugin's own code runs from one), the main checkout its marker records
    (export_checkout). RefreshError when an export names no checkout that still exists."""
    if not os.path.isfile(os.path.join(kit, MARKER)):
        return kit
    checkout = export_checkout(kit)
    if not checkout:
        raise RefreshError('%s is a plugin export and its marker names no source checkout that '
                           'still exists, so its refs cannot be read' % kit)
    return checkout


def tracking_main(kit, remote):
    """(sha of the kit's refs/remotes/<remote>/main, None), ('', None) without one, (None, why)."""
    return refs.ref_commit('refs/remotes/%s/%s' % (remote, MAIN), kit)


def _count(kit, sha):
    out, why = cmd.run_text(['rev-list', '--count', sha], kit)
    if out is None or not out.isdigit():
        raise RefreshError('cannot count the commits of %s: %s' % (sha[:12], why or repr(out)))
    return int(out)


def _tree(kit, sha):
    """[(mode, type, oid, path)] of every entry at sha, recursively."""
    out, why = cmd.run(['ls-tree', '-r', '-z', '--full-tree', sha], kit)
    if out is None:
        raise RefreshError('cannot list the files of %s: %s' % (sha[:12], why))
    entries = []
    for rec in out.split(b'\0'):
        if not rec:
            continue
        head, _, path = rec.partition(b'\t')
        parts = head.split()
        if len(parts) != 3 or not path:
            raise RefreshError('cannot read a git ls-tree line: %r' % rec[:200])
        entries.append((parts[0].decode('ascii'), parts[1].decode('ascii'), parts[2].decode('ascii'),
                        path.decode('utf-8')))
    return entries


def kit_version(manifest):
    """The `version` in plugin.json bytes, or RefreshError."""
    try:
        data = json.loads(manifest.decode('utf-8-sig'))
    except (UnicodeDecodeError, ValueError) as e:
        raise RefreshError('the kit\'s %s is not JSON: %s' % (MANIFEST, e))
    v = data.get('version') if isinstance(data, dict) else None
    if not isinstance(v, str) or not v:
        raise RefreshError('the kit\'s %s has no version' % MANIFEST)
    return v


def stamped_version(base, count):
    """`<kit version>-main.<commit count>`: rises with every push to main, no commit needed."""
    return '%s-main.%d' % (base, count)


def stamp(manifest, version):
    """plugin.json bytes with its one `version` value replaced; every other byte (a BOM, the
    layout) kept. RefreshError when the file does not hold exactly one."""
    found = _VERSION.findall(manifest)
    if len(found) != 1:
        raise RefreshError('%s holds %d "version" keys, not one' % (MANIFEST, len(found)))
    return _VERSION.sub(lambda m: m.group(1) + version.encode('utf-8') + m.group(3), manifest)


def _safe_rel(path):
    """True for a tree path that writes one file inside the export folder on Windows: relative,
    `/`-separated, no `\\` or `:`, no empty, `.` or `..` part, no part ending in a dot or a space
    (Windows drops those), and no reserved device name (CON, NUL, COM1, ...; with any extension)."""
    if '\\' in path or ':' in path or os.path.isabs(path):
        return False
    for p in path.split('/'):
        if p in ('', '.', '..') or p[-1] in '. ':
            return False
        if p.split('.')[0].rstrip(' ').upper() in _RESERVED:
            return False
    return True


def _is_link(path):
    """True for a symlink, a junction or any other reparse point (or a path lstat cannot read)."""
    try:
        st = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def _rmtree(path):
    """shutil.rmtree, clearing a read-only bit on the way. A failure other than removing one entry
    (rmtree refusing a link, a listing error) is raised, never passed over."""
    def retry(func, p, exc):
        if func not in (os.unlink, os.rmdir, os.remove):
            raise exc
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=lambda func, p, info: retry(func, p, info[1]))


def folder_name(sha):
    return sha[:12]


def platform_manifests(paths, platform):
    """{manifest path: the path whose bytes the export writes there} for `platform`: each
    PLATFORM_FILES manifest among `paths` (`<dir>/hooks/hooks.json`, `<dir>/mcp.json`,
    MANIFEST) with its `<name>.<platform>.json` beside it (the host renderers write one per
    platform; docs/content.md "Manifests"). Windows: none, the files the plugin names are its own."""
    if platform == 'windows':
        return {}
    have = set(paths)
    swap = {}
    for path in have:
        if any(path == name or path.endswith('/' + name) for name in PLATFORM_FILES):
            other = path[:-len('.json')] + '.%s.json' % platform
            if other in have:
                swap[path] = other
    return swap


def export(kit, sha, root, now=None, made_by='tools/plugin_refresh.py', platform=None):
    """<root>/<sha12>: the tracked files at `sha` but the top-level tests/ (LEFT_OUT: no tool reads
    the kit's tests from an export, and they were half its size), plugin.json's version stamped,
    each per-platform manifest holding the running `platform`'s file (platform_manifests; a
    swapped plugin.json is stamped too), and the marker (`made_by`: the tool that wrote it;
    `checkout`: the main checkout of `kit`'s repository, left out when git cannot place it). Written into <sha12>.tmp and renamed, so a folder by the
    final name is always whole. An existing folder whose marker names `sha` is kept as it is.
    Returns (folder, version)."""
    name = folder_name(sha)
    dest = os.path.join(root, name)
    have = read_marker(dest)
    if have and have.get('sha') == sha:
        return dest, have.get('version')
    if os.path.exists(dest):
        raise RefreshError('%s exists and is not an export of %s; move it away first' % (dest, sha[:12]))
    entries = [e for e in _tree(kit, sha) if e[3].split('/')[0] != LEFT_OUT]
    blobs = [e for e in entries if e[1] == 'blob' and e[0] != '120000']
    bad = [e[3] for e in blobs if not _safe_rel(e[3])]
    if bad:
        raise RefreshError('the tree of %s holds a path that cannot be written safely: %r' % (sha[:12], bad[0]))
    data, why = batch.cat_file(sorted({e[2] for e in blobs}), kit)
    if data is None:
        raise RefreshError('cannot read the files of %s: %s' % (sha[:12], why))
    manifest = next((data.get(e[2]) for e in blobs if e[3] == MANIFEST), None)
    if not manifest:
        raise RefreshError('%s has no %s' % (sha[:12], MANIFEST))
    version = stamped_version(kit_version(manifest[1]), _count(kit, sha))
    oids = {e[3]: e[2] for e in blobs}
    swap = platform_manifests(oids, kitplatform.NAME if platform is None else platform)
    tmp = dest + '.tmp'
    if os.path.lexists(tmp):
        _rmtree(tmp)
    os.makedirs(tmp)
    for mode, _typ, oid, path in blobs:
        oid = oids[swap[path]] if path in swap else oid
        got = data.get(oid)
        if got is None:
            raise RefreshError('git has no blob %s for %s' % (oid[:12], path))
        body = got[1]
        if path == MANIFEST:
            body = stamp(body, version)
        full = os.path.join(tmp, *path.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as fh:
            fh.write(body)
    skipped = [e[3] for e in entries if e not in blobs]
    marker = {'sha': sha, 'version': version, 'exported_at': _stamp(now),
              'skipped': skipped, 'made_by': made_by}
    checkout = repo.main_checkout_toplevel(kit)
    if checkout:
        marker['checkout'] = checkout
    with open(os.path.join(tmp, MARKER), 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(marker, fh, indent=2)
        fh.write('\n')
    os.rename(tmp, dest)
    return dest, version


def read_marker(folder):
    """The marker of an export folder, or None (no folder, no marker, unreadable)."""
    try:
        with open(os.path.join(folder, MARKER), encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def export_checkout(folder):
    """The kit main checkout an export folder was made from: its marker's `checkout`, when that
    names a folder holding a .git folder (a main checkout; a linked worktree's .git is a file).
    None for a folder with no marker, an older marker without the key, or a checkout that has
    moved or gone. kitlib.gate.pin pins a tool started from the export through it."""
    marker = read_marker(folder)
    checkout = marker.get('checkout') if marker else None
    if not isinstance(checkout, str) or not checkout:
        return None
    if not os.path.isdir(os.path.join(checkout, '.git')):
        return None
    return checkout


def default_kit(folder):
    """(kit checkout, None) or (None, why): tools/plugin_refresh.py's --kit default for a tool in
    `folder`. A checkout is its own kit. An export (it holds MARKER and has no .git) defaults to
    the main checkout its marker records (export_checkout); with none, it is refused, since the
    export cannot read the remote's main."""
    if not os.path.isfile(os.path.join(folder, MARKER)):
        return folder, None
    checkout = export_checkout(folder)
    if checkout:
        return checkout, None
    return None, '%s: this is an export; pass --kit <kit checkout>' % folder


# ---------------------------------------------------------------- the marketplace and state

def _write_json(path, data):
    """Write `data` as JSON (no BOM) through a temp file and os.replace: a reader sees the old file
    or the new one, never half of one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(data, fh, indent=2)
        fh.write('\n')
    os.replace(tmp, path)


def marketplace_path(root):
    return os.path.join(root, *MARKETPLACE.split('/'))


def write_marketplace(root, folder):
    """<root>/.claude-plugin/marketplace.json: the marketplace `pm-kit` whose plugin `pm-kit` is
    `./<folder>`. The names never change, so agent names (pm-kit:<agent>) and allow entries do not."""
    _write_json(marketplace_path(root), {
        'name': NAME,
        'description': 'The PM kit\'s plugin, exported from pushed kit main (docs/plugin-source.md).',
        'owner': {'name': 'PM kit'},
        'plugins': [{'name': NAME, 'source': './%s' % folder,
                     'description': 'The PM kit: agents, skills and the machine lock and hub MCP servers.'}],
    })


def current_folder(root):
    """The folder name marketplace.json names, or None (no file, unreadable, another shape)."""
    try:
        with open(marketplace_path(root), encoding='utf-8-sig') as fh:
            data = json.load(fh)
        for p in data.get('plugins') or []:
            if p.get('name') == NAME and isinstance(p.get('source'), str):
                src = p['source']
                return src[2:] if src.startswith('./') else src
    except (OSError, ValueError, AttributeError, TypeError):
        return None
    return None


def read_state(root):
    """state.json as a dict, None when there is none; StateError when it cannot be read."""
    path = os.path.join(root, STATE)
    try:
        with open(path, encoding='utf-8-sig') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise StateError('cannot read %s: %s' % (path, e))
    if not isinstance(data, dict) or not isinstance(data.get('folders', {}), dict):
        raise StateError('%s is not a state object' % path)
    data.setdefault('folders', {})
    return data


def write_state(root, state):
    _write_json(os.path.join(root, STATE), state)


def _when(text):
    """A marker's exported_at as a naive local datetime, or None."""
    try:
        when = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return when.astimezone().replace(tzinfo=None) if when.tzinfo else when


# ---------------------------------------------------------------- what is in use

InUse = collections.namedtuple('InUse', 'texts named errors')
# texts: lower-case text that may name a folder (each process's command line and program, each
# scheduled task's definition); named: <sha12>s named outright (pin leases); errors: why each read
# that failed failed. Prune counts what is in use as whole only when errors is empty.


def _processes():
    from kitlib.pmstart import list_processes
    return list_processes(timeout=LIST_TIMEOUT)


def _tasks():
    """[the text of every registered scheduled task's definition] from `schtasks /Query /XML ONE`:
    program, arguments and working folder alike. RuntimeError when schtasks fails or times out."""
    import xml.etree.ElementTree as ET
    from kitlib.pmstart import consoletasks
    from kitlib.proc import spawn
    try:
        r = spawn.run(['schtasks', '/Query', '/XML', 'ONE'], capture_output=True, timeout=LIST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError('schtasks /Query /XML timed out after %s s' % LIST_TIMEOUT)
    if r.returncode != 0:
        said = ((r.stdout or b'') + (r.stderr or b'')).decode('oem', errors='replace').strip()
        raise RuntimeError('schtasks /Query /XML exit %d: %s' % (r.returncode, said[:200] or 'no output'))
    return [' '.join(ET.fromstring(consoletasks.xml_input(r.stdout or b'')).itertext())]


def _leases(root):
    """The <sha12>s the pin leases name (<root>/pins/leases/<sha12>-<pid>.json, kitlib.gate.pin),
    live or not: a dead one keeps its export only until kitlib.gate.pin prunes the lease. No
    leases folder: none. OSError when the folder cannot be listed."""
    try:
        names = os.listdir(os.path.join(root, 'pins', 'leases'))
    except FileNotFoundError:
        return set()
    return {n[:12] for n in names if _SHA12.match(n[:12])}


def read_in_use(root, procs=None, tasks=None):
    """InUse: the command line and program of every running process (one Win32_Process read,
    kitlib.pmstart.list_processes), the definition of every scheduled task (`_tasks`) and the
    pin leases under `root`. A read that fails adds its error and the others still run. An empty
    process list (None or []) is a failed read: a real one always holds the reader itself, and
    kitlib.pmstart.list_processes answers [] when PowerShell printed nothing. procs and tasks:
    the tests' stand-ins for the two reads."""
    texts, named, errors = [], set(), []
    try:
        listed = (procs or _processes)()
        if not listed:
            raise RuntimeError('it came back empty')
        for p in listed:
            for key in ('CommandLine', 'ExecutablePath'):
                v = p.get(key)
                if v:
                    texts.append(str(v).lower())
    except Exception as e:
        errors.append('the process list could not be read (%s: %s)' % (type(e).__name__, e))
    try:
        texts.extend(str(t).lower() for t in (tasks or _tasks)() or ())
    except Exception as e:
        errors.append('the scheduled tasks could not be read (%s: %s)' % (type(e).__name__, e))
    try:
        named |= _leases(root)
    except OSError as e:
        errors.append('the pin leases could not be read (%s)' % e)
    return InUse(texts, named, errors)


def in_use_sha12s(u):
    """Every <sha12> `u` names: a path part of 12 hex digits (not the tail of a uuid), or a lease."""
    found = set(u.named)
    for t in u.texts:
        found.update(_NAMED.findall(t))
    return found


def _version_named(u, version):
    """True when a text of `u` names <...>\\pm-kit\\pm-kit\\<version> (a plugins cache folder)."""
    pat = re.compile(r'[\\/]%s[\\/]%s[\\/]%s' % (NAME, NAME, re.escape(version.lower())) + _ENDS)
    return any(pat.search(t) for t in u.texts)


def _read(in_use):
    """(InUse or None, why it is not whole or None) from the reader `in_use` (None: not read)."""
    if in_use is None:
        return None, 'nothing was read'
    try:
        u = in_use()
    except Exception as e:
        return None, '%s: %s' % (type(e).__name__, e)
    return u, '; '.join(u.errors) or None


def _claim_remove(full):
    """Remove folder `full` by claim: renamed aside to <name>.pruning-<pid> first, which Windows
    refuses while a file in it is open, so a folder in use is kept whole. A claim a stopped run
    left is removed as it is. None when removed, else why not. A claimed folder that cannot be
    removed whole stays claimed, and the next prune removes the rest."""
    name = os.path.basename(full)
    claimed = full
    if not _CLAIMED.match(name):
        claimed = '%s%s%d' % (full, CLAIM, os.getpid())
        try:
            os.rename(full, claimed)
        except OSError as e:
            return 'in use: it could not be claimed (%s)' % (e.strerror or e)
    try:
        _rmtree(claimed)
    except OSError as e:
        return 'claimed as %s but not wholly removed (%s); the next prune removes the rest' % (
            os.path.basename(claimed), e)
    return None


def prune(root, keep=KEEP, now=None, young_days=None, in_use=None, out=None):
    """Remove old export folders, each by claim (`_claim_remove`). Never removed: the folder
    marketplace.json names (the plugin loads from it, in place or by a copy at the next update),
    the `keep` newest by their marker's exported_at, every export a running process, a scheduled
    task or a pin lease names (`in_use`, a reader returning InUse, as read_in_use does), and every
    export replaced less than `young_days` ago, or whose dates cannot be read (a session whose MCP
    server has exited still runs hooks from its folder, and no process names it). `young_days`
    defaults to IN_USE_YOUNG_DAYS when `in_use` read everything without an error, else YOUNG_DAYS:
    what cannot be read counts as possibly in use. An export is dated by when it was replaced, the
    exported_at of the next newer export, not by its own exported_at: one that was current for
    weeks and then replaced by a few quick landings may still be in use. An unreadable exported_at
    counts as newer than every readable one. Only a <sha12> folder holding the marker is a
    candidate; a link or junction is never one, and anything else in the root stays. A
    <sha12>.tmp folder other than the named folder's is a half-written export left by a run that
    stopped (prune runs under refresh.pid, so no export is being written), and a <sha12>.pruning-
    <pid> folder is a claim a stopped prune left: both are removed too. `out` gets a line on what
    the in-use read kept or why it was not whole. Returns ([removed folder], [(folder, why) not
    removed])."""
    say = out or (lambda line: None)
    u, why = _read(in_use)
    used = in_use_sha12s(u) if u else set()
    if young_days is None:
        young_days = YOUNG_DAYS if why else IN_USE_YOUNG_DAYS
    if why and in_use is not None:
        say('prune: what is in use could not be read whole (%s): keeping every export replaced '
            'less than %d days ago' % (why, young_days))
    current = current_folder(root)
    now = now or datetime.datetime.now()
    found, stale = [], []
    try:
        names = os.listdir(root)
    except OSError:
        return [], []
    for n in names:
        full = os.path.join(root, n)
        if not os.path.isdir(full) or _is_link(full):
            continue
        if _CLAIMED.match(n) and _SHA12.match(n[:12]):
            stale.append(n)
            continue
        tmp = _TMP.match(n)
        if tmp:
            if tmp.group(1) != current:
                stale.append(n)
            continue
        if not _SHA12.match(n):
            continue
        marker = read_marker(full)
        if marker is None:
            continue
        found.append((_when(str(marker.get('exported_at') or '')), n))
    # newest first; an unreadable date first of all (it may be the newest)
    found.sort(key=lambda w: (w[0] is None, w[0] or datetime.datetime.min, w[1]), reverse=True)
    spare = {n for _, n in found[:keep]}
    if current:
        spare.add(current)
    for i, (when, n) in enumerate(found):
        replaced = found[i - 1][0] if i else None      # the exported_at of the next newer export
        if when is None or replaced is None or now - replaced < datetime.timedelta(days=young_days):
            spare.add(n)
    held = sorted(n for _, n in found if n in used and n not in spare)
    if held:
        say('prune: kept, a running process, scheduled task or pin lease names it: %s' % ', '.join(held))
    spare.update(used)
    removed, failed = [], []
    for n in [n for _, n in found if n not in spare] + sorted(stale):
        full = os.path.join(root, n)
        why_not = _claim_remove(full)
        if why_not is None:
            removed.append(full)
        else:
            failed.append((full, why_not))
    return removed, failed


def cache_dir(folder):
    """Account folder `folder`'s plugins cache of the kit: <folder>\\plugins\\cache\\pm-kit\\pm-kit,
    one <version> folder per install Claude Code copied there."""
    return os.path.join(folder, 'plugins', 'cache', NAME, NAME)


def prune_caches(folders, in_use, apply=False, now=None):
    """Old versions in each account folder's plugins cache (cache_dir): [(version folder, action,
    why)], action 'kept', 'would' (a dry run), 'removed' or 'failed'. Kept: every version when
    `in_use` (a reader, as for prune) could not read everything, or the account's install record
    (claude.installed_path) is missing, unreadable or names no install, or a link lies on the way
    to the cache; the version the install record's installPath names; the newest, and each
    replaced (the next newer version folder's modified time) less than IN_USE_YOUNG_DAYS ago, or
    whose time cannot be read; one a running process or scheduled task names; a link or
    junction. Every other version folder goes, by claim (`_claim_remove`), when `apply`; a claim
    a stopped run left goes too."""
    now = now or datetime.datetime.now()
    u, unread = _read(in_use)
    rows = []
    for f in folders:
        cache = cache_dir(f)
        try:
            names = sorted(os.listdir(cache))
        except OSError:
            continue                    # no cache: nothing to prune
        hops = [os.path.join(f, 'plugins'), os.path.join(f, 'plugins', 'cache'),
                os.path.dirname(cache), cache]
        linked = any(_is_link(h) for h in hops)
        installed = _host().installed_path(f)
        vers, claims = [], []
        for n in names:
            p = os.path.join(cache, n)
            if _is_link(p):
                rows.append((p, 'kept', 'a link or junction, never followed'))
                continue
            if not os.path.isdir(p):
                continue
            if _CLAIMED.match(n):
                claims.append(n)        # a claim a stopped run left: never dates a version
                continue
            try:
                t = datetime.datetime.fromtimestamp(os.stat(p).st_mtime)
            except (OSError, ValueError, OverflowError):
                t = None
            vers.append((t, n))
        vers.sort(key=lambda w: (w[0] is None, w[0] or datetime.datetime.min, w[1]), reverse=True)
        todo = [(t, n, vers[i - 1][0] if i else None) for i, (t, n) in enumerate(vers)]
        todo += [(None, n, None) for n in claims]
        for t, n, replaced in todo:
            p = os.path.join(cache, n)
            if unread:
                why = 'what is in use could not be read whole (%s)' % unread
            elif linked:
                why = 'a link or junction lies on the way to %s' % cache
            elif installed is None:
                why = ('the install record %s is missing, cannot be read or names no %s install'
                       % (os.path.join(f, 'plugins', 'installed_plugins.json'), PLUGIN_ID))
            elif _under(installed, p):
                why = 'the install record names it'
            elif _CLAIMED.match(n):
                why = None              # a claim a stopped run left
            elif t is None or replaced is None:
                why = 'the newest, or its time cannot be read'
            elif now - replaced < datetime.timedelta(days=IN_USE_YOUNG_DAYS):
                why = 'replaced less than %d day ago' % IN_USE_YOUNG_DAYS
            elif _version_named(u, n):
                why = 'a running process or scheduled task names it'
            else:
                why = None
            if why:
                rows.append((p, 'kept', why))
            elif not apply:
                rows.append((p, 'would', None))
            else:
                why_not = _claim_remove(p)
                rows.append((p, 'removed', None) if why_not is None else (p, 'failed', why_not))
    return rows


# ---------------------------------------------------------------- one refresh at a time

class Busy(Exception):
    """Another refresh holds refresh.pid."""


def _own():
    from kitlib.proc import process
    return os.getpid(), process.own_ctime()


def _alive(pid, ctime):
    from kitlib.proc import process
    return process.alive(pid, ctime)


class RefreshLock(object):
    """<root>/refresh.pid, created exclusively, holding {pid, ctime}. A file whose process is gone
    (pid dead, or its start time differs: a reused pid) is stale and taken over. An empty or
    unreadable file is one a starting refresh has made but not yet written: live until its mtime
    is more than LOCK_GRACE_S old, stale after."""

    def __init__(self, root, own=None, alive=None, clock=None):
        self.path = os.path.join(root, PIDFILE)
        self.own = own or _own()
        self.alive = alive or _alive
        self.clock = clock or time.time
        self.held = False

    def holder(self):
        """{pid, ctime} of a live holder (pid None: a file not yet written), or None (no file, or
        a stale one)."""
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            pid, ctime = int(data['pid']), data.get('ctime')
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            try:
                age = self.clock() - os.path.getmtime(self.path)
            except FileNotFoundError:
                return None
            except OSError:
                age = 0
            return None if age > LOCK_GRACE_S else {'pid': None, 'ctime': None}
        return {'pid': pid, 'ctime': ctime} if self.alive(pid, ctime) else None

    def acquire(self):
        for _ in range(2):
            try:
                with open(self.path, 'x', encoding='utf-8', newline='\n') as fh:
                    json.dump({'pid': self.own[0], 'ctime': self.own[1]}, fh)
                self.held = True
                return
            except FileExistsError:
                h = self.holder()
                if h is not None:
                    who = 'pid %d' % h['pid'] if h['pid'] is not None else 'its pid not yet written'
                    raise Busy('a refresh is already running (%s, %s)' % (who, self.path))
                try:
                    os.remove(self.path)        # stale: its process is gone
                except FileNotFoundError:
                    pass
        raise Busy('cannot take %s' % self.path)

    def release(self):
        if not self.held:
            return
        self.held = False
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            if data.get('pid') == self.own[0] and data.get('ctime') == self.own[1]:
                os.remove(self.path)
        except (OSError, ValueError, AttributeError):
            pass


def lock_live(root, alive=None):
    """True when a live refresh holds <root>/refresh.pid."""
    return RefreshLock(root, own=(0, None), alive=alive).holder() is not None


# ---------------------------------------------------------------- the run

def _stamp(now=None):
    return (now or datetime.datetime.now()).isoformat(timespec='seconds')


def old_cache(config_dir, version):
    """Where an install of the kit checkout as its own marketplace kept its copy: the plugins
    cache of `config_dir`, marketplace pm-kit, plugin pm-kit, `version`. A path made from names,
    never looked up on disk."""
    return os.path.join(config_dir, 'plugins', 'cache', NAME, NAME, version)


UPDATE_STEPS = (['plugin', 'marketplace', 'update', NAME], ['plugin', 'update', PLUGIN_ID])


def migrate_steps(root):
    return (['plugin', 'marketplace', 'remove', NAME], ['plugin', 'marketplace', 'add', root],
            ['plugin', 'install', PLUGIN_ID, '-s', 'user'])


def folder_key(folder):
    """The key an account folder is matched by (state.json keys, --folder names)."""
    return _norm(folder)


def _record(state, folder):
    """state.json's record of `folder` ({} when none)."""
    k = folder_key(folder)
    for f, r in (state or {}).get('folders', {}).items():
        if folder_key(f) == k and isinstance(r, dict):
            return r
    return {}


def migrated(state, folder):
    """True when `folder`'s last migrate succeeded (its record holds `migrated: true`)."""
    return _record(state, folder).get('migrated') is True


def _carry(state):
    """The folder records a new export's state starts with: each migrated folder, as migrated."""
    return {f: {'migrated': True} for f, r in (state or {}).get('folders', {}).items()
            if isinstance(r, dict) and r.get('migrated') is True}


def _set_record(state, folder, rec):
    k = folder_key(folder)
    for f in [f for f in state['folders'] if folder_key(f) == k]:
        del state['folders'][f]
    state['folders'][folder] = rec


def pick_folders(folders, wanted):
    """([account folder], [name]): the folders of `folders` that `wanted` (--folder) names, each
    once and in the order named, and the names that are not one of `folders`. Nothing wanted:
    every folder."""
    if not wanted:
        return list(folders), []
    by = {folder_key(f): f for f in folders}
    picked, bad = [], []
    for w in wanted:
        f = by.get(folder_key(w))
        if f is None:
            bad.append(w)
        elif f not in picked:
            picked.append(f)
    return picked, bad


def migrate_cmd(folder):
    return '%s --folder "%s"' % (refresh_cmd(True), folder)


def refresh(kit, s, folders, out, apply=False, migrate=False, runner=None, now=None, own=None,
            alive=None, in_use=None, copies=None):
    """One refresh (tools/plugin_refresh.py). `folders` are the account folders to run in (all of
    them, or those --folder named); `runner` is kitlib.pluginsource.claude.Runner (or a test's);
    `in_use` is the prune's reader of what is in use (prune; None: the 7-day rule). `copies` are
    the hosts' plugin-copy steps (tools/_host.py plugin_copies), each run as step(export folder,
    out, apply) after the account folders, also when they had nothing to do: a step prints its own
    lines and never sets the exit code; None or []: none. After an update that went through, the
    old versions in each account folder's plugins cache are pruned (_prune_old_caches).
    Without `migrate`, a folder whose state record does not say `migrated: true` is refused with
    a line naming the migrate command for it, and the others still run. Dry run (apply False):
    prints the plan and writes nothing. Returns the exit code: 0 done or nothing to do, 1 refused
    (the whole run or one folder) or a folder failed."""
    root = s.export_dir
    try:
        sha = remote_main(kit, s.remote)
        state = read_state(root)
    except (RefreshError, StateError) as e:
        out('REFUSED: %s' % e)
        return 1
    name = folder_name(sha)
    exported = read_marker(os.path.join(root, name))
    fresh = bool(exported and exported.get('sha') == sha and current_folder(root) == name
                 and state and state.get('sha') == sha)
    refused = [] if migrate else [f for f in folders if not migrated(state, f)]
    if migrate:
        todo = list(folders)
    else:
        todo = [f for f in folders if f not in refused
                and not (fresh and _record(state, f).get('rc') == 0)]
    steps = migrate_steps(root) if migrate else UPDATE_STEPS
    out('kit %s/%s: %s' % (s.remote, MAIN, sha[:12]))
    for f in refused:
        out('REFUSED %s: not migrated to the export yet: %s' % (f, migrate_cmd(f)))
    if fresh and not todo:
        if not refused:
            out('pm-kit plugin is current: %s is exported and every account folder named refreshed; '
                'nothing to do' % name)
        if copies:
            _copies_locked(s, name, sha, copies, out, apply, own, alive)
        return 1 if refused else 0
    if not apply:
        out('DRY RUN (nothing written; --apply makes these):')
        if not fresh:
            out('  export %s into %s, then point %s at it' % (sha[:12], os.path.join(root, name),
                                                         marketplace_path(root)))
            out('  prune export folders other than that one, the %d newest, those a running process, '
                'scheduled task or pin lease names, and those replaced less than %d day ago (%d days '
                'when what is in use cannot be read whole)' % (KEEP, IN_USE_YOUNG_DAYS, YOUNG_DAYS))
        for f in todo:
            for st in steps:
                out('  %s: claude %s' % (f, ' '.join(st)))
        if not todo:
            out('  no account folder to run claude in')
        if migrate:
            _old_cache_lines(kit, sha, folders, out)
        if copies:
            _copies(s, name, sha, copies, out, False)
        return 1 if refused else 0
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as e:
        out('REFUSED: cannot make the export root %s: %s' % (root, e))
        return 1
    lock = RefreshLock(root, own=own, alive=alive)
    try:
        lock.acquire()
    except Busy as e:
        out('REFUSED: %s' % e)
        return 1
    try:
        code = _apply(kit, s, sha, state, fresh, todo, steps, migrate, folders, runner, out, now,
                      in_use)
        if copies:
            _copies(s, name, sha, copies, out, True)
    finally:
        lock.release()
    return 1 if refused else code


def _copies(s, name, sha, copies, out, apply):
    """Run each plugin-copy step with export <name>'s folder. With apply the caller holds the
    refresh lock, and an export that is not there whole (the export step failed) is a line per
    step, not a write. A step that raises is a line: it never fails the account folders' result."""
    folder = os.path.join(s.export_dir, name)
    whole = not apply or (read_marker(folder) or {}).get('sha') == sha
    for step in copies:
        label = getattr(step, 'label', 'plugin copy')
        if not whole:
            out('%s: not refreshed: there is no export of %s at %s' % (label, sha[:12], folder))
            continue
        try:
            step(folder, out, apply)
        except Exception as e:                   # never fail the refresh over a host's copy
            out('%s: not refreshed: %s: %s' % (label, type(e).__name__, e))


def _copies_locked(s, name, sha, copies, out, apply, own, alive):
    """_copies when the account folders had nothing to do: with apply, under the refresh lock
    (busy: a line per step, and the copies are left for the refresh holding it)."""
    if not apply:
        _copies(s, name, sha, copies, out, False)
        return
    lock = RefreshLock(s.export_dir, own=own, alive=alive)
    try:
        lock.acquire()
    except Busy as e:
        for step in copies:
            out('%s: not refreshed: %s' % (getattr(step, 'label', 'plugin copy'), e))
        return
    try:
        _copies(s, name, sha, copies, out, True)
    finally:
        lock.release()


def _apply(kit, s, sha, state, fresh, todo, steps, migrate, folders, runner, out, now, in_use=None):
    root = s.export_dir
    name = folder_name(sha)
    step = 'export'
    try:
        folder, version = export(kit, sha, root, now)
        out('exported %s: %s (version %s)' % (sha[:12], folder, version))
        step = 'marketplace write'
        write_marketplace(root, name)
        out('marketplace %s names ./%s' % (marketplace_path(root), name))
        if not fresh:
            step = 'state write'
            state = {'sha': sha, 'version': version, 'folder': name, 'exported_at': _stamp(now),
                     'folders': _carry(state)}
            write_state(root, state)
        step = 'prune'
        removed, failed = prune(root, now=now, in_use=in_use, out=out)
    except (RefreshError, OSError, ValueError) as e:
        out('REFUSED: the %s failed: %s' % (step, e))
        return 1
    for r in removed:
        out('pruned %s' % r)
    for f, why in failed:
        out('WARNING: could not remove %s: %s' % (f, why))
    if migrate:
        _old_cache_lines(kit, sha, folders, out)
    if not todo:
        _prune_old_caches(folders, state, in_use, migrate, out, now)
        return 0
    if runner is None or not runner.available():
        out('REFUSED: claude is not on PATH: the export is made, no account folder was refreshed')
        return 1
    code = 0
    for f in todo:
        rc, cmd_line, text = 0, None, ''
        for st in steps:
            rc, text = runner.run(st, f)
            cmd_line = ' '.join(st)
            if rc != 0 and not (migrate and st[:3] == ['plugin', 'marketplace', 'remove']):
                break
            rc = 0
        rec = {'rc': rc, 'at': _stamp(now), 'step': cmd_line,
               'migrated': (rc == 0) if migrate else True}
        _set_record(state, f, rec)
        try:
            write_state(root, state)
        except (OSError, ValueError) as e:
            out('REFUSED: the state write failed after %s: %s' % (f, e))
            return 1
        if rc == 0:
            out('%s: %s' % (f, 'migrated' if migrate else 'refreshed'))
        else:
            code = 1
            out('FAILED %s: claude %s exited %s: %s' % (f, cmd_line, rc, (text or '').strip()[-400:]))
    if code == 0:
        _prune_old_caches(folders, state, in_use, migrate, out, now)
    return code


def _prune_old_caches(folders, state, in_use, migrate, out, now):
    """After an update that went through, the old versions in the plugins cache of each account
    folder that `state` records migrated and refreshed with rc 0 go; a refused folder, not yet
    migrated, is never touched. prune_caches runs with apply, every guard of it kept: the
    installed version, the newest, one replaced less than IN_USE_YOUNG_DAYS ago, one in use, a
    link. Prints a line per version removed or failed, never the kept ones, and changes no exit
    code. A migrate run removes none: it only lists the old caches (_old_cache_lines)."""
    if migrate:
        return
    done = [f for f in folders if migrated(state, f) and _record(state, f).get('rc') == 0]
    if not done:
        return
    try:
        rows = prune_caches(done, in_use, apply=True, now=now)
    except (OSError, ValueError) as e:
        out('WARNING: the old plugin caches were not pruned: %s' % e)
        return
    for p, action, why in rows:
        if action == 'removed':
            out('pruned cache %s' % p)
        elif action == 'failed':
            out('WARNING: could not remove %s: %s' % (p, why))


def ensure_export(kit, s, now=None, own=None, alive=None, in_use=None):
    """The export root, holding an export of the remote's main that marketplace.json names: made
    when missing, kept when current. Runs no claude command and records no account folder (an
    installer then adds the root as a marketplace); a new export's state keeps each folder's
    migrated flag. A state.json that cannot be read raises StateError before anything is
    written, so its migrated flags are never overwritten. `in_use`: as for prune. Returns (root, sha); RefreshError,
    StateError, Busy or OSError."""
    sha = remote_main(kit, s.remote)
    root = s.export_dir
    name = folder_name(sha)
    have = read_marker(os.path.join(root, name))
    if have and have.get('sha') == sha and current_folder(root) == name:
        return root, sha
    os.makedirs(root, exist_ok=True)
    lock = RefreshLock(root, own=own, alive=alive)
    lock.acquire()
    try:
        state = read_state(root)        # StateError: refuse rather than drop the flags
        _folder, version = export(kit, sha, root, now)
        write_marketplace(root, name)
        if not state or state.get('sha') != sha:
            write_state(root, {'sha': sha, 'version': version, 'folder': name,
                               'exported_at': _stamp(now), 'folders': _carry(state)})
        prune(root, now=now, in_use=in_use)
    finally:
        lock.release()
    return root, sha


def _old_cache_lines(kit, sha, folders, out):
    """The copies an install from the kit checkout left (docs/plugin-source.md "Migrating"): named,
    never looked at or removed."""
    try:
        data, why = batch.cat_file(['%s:%s' % (sha, MANIFEST)], kit)
        base = kit_version(data['%s:%s' % (sha, MANIFEST)][1]) if data else None
    except (RefreshError, KeyError, TypeError):
        base = None
    if not base:
        out('old caches: cannot read the kit version at %s; see docs/plugin-source.md "Migrating"' % sha[:12])
        return
    out('old caches (a migrate never removes them; after the migrate is verified, '
        'plugin_refresh.py --prune-caches lists what it would remove, docs/plugin-source.md "Old caches"):')
    for f in folders:
        out('  %s' % old_cache(f, base))


# ---------------------------------------------------------------- pm_start and pm_watch

def _host():
    from kitlib.pluginsource import claude
    return claude


def _folders(folders):
    if folders is None:
        folders = _host().account_folders()
    return list(folders)


def account_folders():
    """This machine's account folders on the plugin's host (the ones `folders=None` means here)."""
    return _folders(None)


def installs(folders=None):
    """[(install path, account folder)]: where each account folder of `folders` (default every
    one) loads the kit plugin from, for the folders that have it installed. Reads each folder's
    install record only."""
    return _host().installs(_folders(folders))


def marketplace_mismatches(export_dir, folders=None, managed=None):
    """Lines for each place pointing the pm-kit marketplace off `export_dir`: each account folder's
    (`folders`, default every one) known marketplaces record and the managed settings file
    `managed` (None: not checked). Reads only; never raises."""
    return _host().marketplace_mismatches(export_dir, _folders(folders), managed=managed)


def unmigrated(state, folders):
    """The account folders of `folders` whose state record does not say `migrated: true`."""
    return [f for f in folders if not migrated(state, f)]


_MACHINE = object()


def _marketplace_lines(export_dir, folders, managed):
    """marketplace_mismatches for current_lines: `managed` _MACHINE is the default host's managed
    settings file (kitlib.machinesetup.extras Host.managed_path). Never raises: a session start
    must not break over a report, so any failure is one 'could not check' line."""
    try:
        if managed is _MACHINE:
            from kitlib.machinesetup.extras import Host
            managed = Host().managed_path()
        return marketplace_mismatches(export_dir, folders, managed=managed)
    except Exception as e:                       # never break a session start over a report
        return ['could not check the pm-kit marketplace paths: %s: %s' % (type(e).__name__, e)]


def current_lines(kit, s, folders=None, managed=_MACHINE):
    """pm_start lines: none when the export is at the kit's refs/remotes/<remote>/main, every
    account folder (`folders`, default claude.account_folders()) is migrated and its last refresh
    succeeded, and the pm-kit marketplace points at the export. The refresh checks read the export
    state only: a folder's last refresh result stands in for what it has installed. No export yet,
    or a folder not migrated: the line names the migrate command, not --apply alone (which refuses
    such a folder). The marketplace check (marketplace_mismatches) reads each migrated folder's
    known marketplaces record and the managed settings file `managed` (default: the machine's when
    `folders` is None, else not checked; None: not checked); it never raises."""
    if managed is _MACHINE and folders is not None:
        managed = None
    try:
        state = read_state(s.export_dir)
    except StateError as e:
        return ['could not check the pm-kit plugin: %s' % e]
    if state is None:
        return ['pm-kit plugin: no export at %s yet: %s' % (s.export_dir, refresh_cmd(True))]
    try:
        main, why = tracking_main(source_kit(kit), s.remote)
    except RefreshError as e:
        return ['could not check the pm-kit plugin: %s' % e]
    if main is None:
        return ['could not check the pm-kit plugin: %s' % why]
    folders = _folders(folders)
    todo = unmigrated(state, folders)
    cmd = refresh_cmd(bool(todo))
    lines = []
    if todo:
        lines.append('pm-kit plugin: not migrated to the export in %s: %s (one folder first: '
                     '--folder DIR)' % (', '.join(todo), refresh_cmd(True)))
    if main and main != state.get('sha'):
        lines.append('pm-kit plugin behind kit main (exported %s, %s/%s %s): %s'
                     % (str(state.get('sha') or '?')[:12], s.remote, MAIN, main[:12], cmd))
    for f in sorted(state['folders']):
        r = state['folders'][f] or {}
        if r.get('migrated') is not True or r.get('rc') == 0:
            continue
        if r.get('rc') is None:     # migrated, carried into this export, never refreshed to it
            lines.append('pm-kit plugin in %s: not refreshed to %s: %s'
                         % (f, str(state.get('sha') or '?')[:12], refresh_cmd()))
        else:
            lines.append('pm-kit plugin refresh failed in %s (claude %s exited %s at %s): %s'
                         % (f, r.get('step') or '?', r.get('rc'), r.get('at') or '?', refresh_cmd()))
    lines.extend(_marketplace_lines(s.export_dir, [f for f in folders if f not in todo], managed))
    return lines


def spawn_refresh(kit, root):
    """Start tools/plugin_refresh.py --apply detached and windowless, its output in <root>/refresh.log."""
    from kitlib.machinesetup.extras import pythonw
    os.makedirs(root, exist_ok=True)
    log = open(os.path.join(root, LOGFILE), 'ab')
    try:
        subprocess.Popen([pythonw(), os.path.join(kit, 'tools', 'plugin_refresh.py'), '--apply'],
                         cwd=kit, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         close_fds=True, creationflags=_DETACHED | _NOWIN)
    finally:
        log.close()


def poll(kit, s, last, spawn=None, alive=None, folders=None):
    """One pm_watch check. Returns (lines, last): an event line the first time the kit's
    refs/remotes/<remote>/main differs from the exported sha (`last` is the main last reported, so
    the same sha is not reported twice), none when current. No export yet, or an account folder
    (`folders`, default claude.account_folders()) not migrated: the line names the migrate command
    and auto_refresh starts nothing. Otherwise, with plugin.auto_refresh on, that first time also
    starts a refresh (`spawn(kit, root)`), unless one holds refresh.pid already. No tracking ref:
    nothing. Raises RefreshError when the tracking ref cannot be read and StateError when
    state.json cannot, so pm_watch counts the failure."""
    main, why = tracking_main(source_kit(kit), s.remote)
    if main is None:
        raise RefreshError('cannot read %s/%s: %s' % (s.remote, MAIN, why))
    if not main:
        return [], last
    state = read_state(s.export_dir)
    if state is not None and main == state.get('sha'):
        return [], None
    if main == last:
        return [], last
    todo = None if state is None else unmigrated(state, _folders(folders))
    if state is None or todo:
        line = 'pm-kit plugin behind kit main %s: %s' % (main[:12], refresh_cmd(True))
        if s.auto_refresh:
            line += (' (auto_refresh: not started: no export yet)' if state is None else
                     ' (auto_refresh: not started: not migrated in %s)' % ', '.join(todo))
        return [line], main
    line = 'pm-kit plugin behind kit main %s: %s' % (main[:12], refresh_cmd())
    if s.auto_refresh:
        if lock_live(s.export_dir, alive):
            line += ' (auto_refresh: a refresh is already running)'
        else:
            try:
                (spawn or spawn_refresh)(kit, s.export_dir)
                line += ' (auto_refresh: started)'
            except OSError as e:
                line += ' (auto_refresh: could not start it: %s)' % e
    return [line], main
