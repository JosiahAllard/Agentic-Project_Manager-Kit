"""The plugin's source: an export of pushed kit main that every account folder's Claude Code
installs from (docs/plugin-source.md).

  remote_main(kit, remote)         the sha the remote's main points at; refuses one not fetched
  export(kit, sha, root)           <root>/<sha12>/: the tracked files at sha, plugin.json stamped
  write_marketplace(root, folder)  <root>/.claude-plugin/marketplace.json names that folder
  prune(root)                      old export folders removed (never the named one, never the 3 newest)
  read_state / write_state         <root>/state.json: {sha, version, folder, exported_at, folders}
  RefreshLock                      <root>/refresh.pid, one refresh at a time
  refresh(...)                     the whole run tools/plugin_refresh.py makes
  current_lines(kit, s)            the pm_start warning lines (behind, failed folder)
  poll(kit, s, last, ...)          the pm_watch check: an event line per new main, and the spawn
                                   of a refresh when plugin.auto_refresh is on

The export holds tracked files only (git ls-tree of the commit, blobs from one cat-file batch),
so no worktree, no .git and nothing untracked reaches a plugin cache. It reads git and writes
only under the export root; it fetches nothing and takes no machine lock.
"""
import collections
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys

from kitlib.git import batch, cmd, refs

NAME = 'pm-kit'                      # the marketplace's and the plugin's name
PLUGIN_ID = 'pm-kit@pm-kit'
MAIN = 'main'
DEFAULT_REMOTE = 'origin'
MARKER = '.pm-kit-export'            # in every export folder: this tool made it (prune reads it)
STATE = 'state.json'
PIDFILE = 'refresh.pid'
LOGFILE = 'refresh.log'
KEEP = 3                             # newest export folders prune keeps, besides the named one
MANIFEST = '.claude-plugin/plugin.json'
MARKETPLACE = '.claude-plugin/marketplace.json'
REFRESH_CMD = 'python tools/plugin_refresh.py --apply'
CONFIG_KEY = 'plugin'
_SHA12 = re.compile(r'^[0-9a-f]{12}$')
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


def check(obj):
    """Errors in the machine config's `plugin` object (kitlib.config.user registers the key as an
    object; its inner keys are checked here)."""
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
        elif k == 'auto_refresh':
            if not isinstance(v, bool):
                errs.append('plugin.auto_refresh must be true or false, not %r' % (v,))
        elif k == 'remote':
            if not isinstance(v, str) or refs.bad_rev(v) or not v.strip():
                errs.append('plugin.remote must be a git remote name, not %r' % (v,))
        else:
            errs.append('plugin.%s is not a known key (export_dir, auto_refresh, remote)' % k)
    return errs


def settings(obj, environ=None):
    """The checked Settings of a `plugin` object (None: every default). The default export_dir is
    %LOCALAPPDATA%\\pm-kit\\plugin; RefreshError when that is needed and LOCALAPPDATA is unset."""
    errs = check(obj)
    if errs:
        raise RefreshError('; '.join(errs))
    obj = obj or {}
    root = obj.get('export_dir')
    if not root:
        env = os.environ if environ is None else environ
        base = env.get('LOCALAPPDATA')
        if not base or not os.path.isabs(base):
            raise RefreshError('LOCALAPPDATA is not set: set plugin.export_dir in the machine config')
        root = os.path.join(base, 'pm-kit', 'plugin')
    return Settings(os.path.normpath(root), obj.get('auto_refresh') is True,
                    obj.get('remote') or DEFAULT_REMOTE)


def load(home=None, environ=None):
    """The machine's plugin Settings (kitlib.config ConfigError for a bad machine config)."""
    from kitlib.config import user
    return settings(user.user_config(home).get(CONFIG_KEY), environ)


def kit_root():
    """The checkout this module is in."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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
    parts = path.split('/')
    return not (os.path.isabs(path) or ':' in path or any(p in ('', '.', '..') for p in parts))


def _rmtree(path):
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def folder_name(sha):
    return sha[:12]


def export(kit, sha, root, now=None):
    """<root>/<sha12>: the tracked files at `sha`, plugin.json's version stamped, and the marker.
    Written into <sha12>.tmp and renamed, so a folder by the final name is always whole. An
    existing folder whose marker names `sha` is kept as it is. Returns (folder, version)."""
    name = folder_name(sha)
    dest = os.path.join(root, name)
    have = read_marker(dest)
    if have and have.get('sha') == sha:
        return dest, have.get('version')
    if os.path.exists(dest):
        raise RefreshError('%s exists and is not an export of %s; move it away first' % (dest, sha[:12]))
    entries = _tree(kit, sha)
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
    tmp = dest + '.tmp'
    if os.path.lexists(tmp):
        _rmtree(tmp)
    os.makedirs(tmp)
    for mode, _typ, oid, path in blobs:
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
              'skipped': skipped, 'made_by': 'tools/plugin_refresh.py'}
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
                     'description': 'The PM kit: agents, skills and the machine lock MCP server.'}],
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


def prune(root, keep=KEEP):
    """Remove old export folders. Never removed: the folder marketplace.json names (the plugin
    loads from it, in place or by a copy at the next update) and the `keep` newest by their
    marker's exported_at (a running session may still load hooks and its MCP server from one).
    Only a <sha12> folder holding the marker is a candidate; anything else in the root stays.
    Returns ([removed folder], [(folder, why) not removed])."""
    current = current_folder(root)
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return [], []
    for n in names:
        full = os.path.join(root, n)
        if not _SHA12.match(n) or not os.path.isdir(full):
            continue
        marker = read_marker(full)
        if marker is None:
            continue
        found.append((str(marker.get('exported_at') or ''), n))
    found.sort(reverse=True)
    spare = {n for _, n in found[:keep]}
    if current:
        spare.add(current)
    removed, failed = [], []
    for _, n in found:
        if n in spare:
            continue
        full = os.path.join(root, n)
        try:
            _rmtree(full)
            removed.append(full)
        except OSError as e:
            failed.append((full, str(e)))
    return removed, failed


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
    (pid dead, or its start time differs: a reused pid) is stale and taken over."""

    def __init__(self, root, own=None, alive=None):
        self.path = os.path.join(root, PIDFILE)
        self.own = own or _own()
        self.alive = alive or _alive
        self.held = False

    def holder(self):
        """{pid, ctime} of a live holder, or None (no file, or a stale one)."""
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            pid, ctime = int(data['pid']), data.get('ctime')
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            return None
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
                    raise Busy('a refresh is already running (pid %d, %s)' % (h['pid'], self.path))
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


def refresh(kit, s, folders, out, apply=False, migrate=False, runner=None, now=None, own=None,
            alive=None):
    """One refresh (tools/plugin_refresh.py). `folders` are the account folders; `runner` is
    kitlib.pluginsource.claude.Runner (or a test's). Dry run (apply False): prints the plan and
    writes nothing. Returns the exit code: 0 done or nothing to do, 1 refused or a folder failed."""
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
    done = (state or {}).get('folders', {}) if fresh else {}
    todo = list(folders) if migrate else [f for f in folders if (done.get(f) or {}).get('rc') != 0]
    steps = migrate_steps(root) if migrate else UPDATE_STEPS
    out('kit %s/%s: %s' % (s.remote, MAIN, sha[:12]))
    if fresh and not todo:
        out('pm-kit plugin is current: %s is exported and every account folder refreshed; nothing to do'
            % name)
        return 0
    if not apply:
        out('DRY RUN (nothing written; --apply makes these):')
        if not fresh:
            out('  export %s into %s, then point %s at it' % (sha[:12], os.path.join(root, name),
                                                         marketplace_path(root)))
            out('  prune export folders other than that one and the %d newest' % KEEP)
        for f in todo:
            for st in steps:
                out('  %s: claude %s' % (f, ' '.join(st)))
        if not todo:
            out('  no account folder to run claude in')
        if migrate:
            _old_cache_lines(kit, sha, folders, out)
        return 0
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
        return _apply(kit, s, sha, state if fresh else None, todo, steps, migrate, folders, runner,
                      out, now)
    finally:
        lock.release()


def _apply(kit, s, sha, state, todo, steps, migrate, folders, runner, out, now):
    root = s.export_dir
    name = folder_name(sha)
    try:
        folder, version = export(kit, sha, root, now)
    except (RefreshError, OSError) as e:
        out('REFUSED: the export failed: %s' % e)
        return 1
    out('exported %s: %s (version %s)' % (sha[:12], folder, version))
    write_marketplace(root, name)
    out('marketplace %s names ./%s' % (marketplace_path(root), name))
    if state is None:
        state = {'sha': sha, 'version': version, 'folder': name, 'exported_at': _stamp(now),
                 'folders': {}}
        write_state(root, state)
    removed, failed = prune(root)
    for r in removed:
        out('pruned %s' % r)
    for f, why in failed:
        out('WARNING: could not remove %s: %s' % (f, why))
    if migrate:
        _old_cache_lines(kit, sha, folders, out)
    if not todo:
        return 0
    if runner is None or not runner.available():
        out('REFUSED: claude is not on PATH: the export is made, no account folder was refreshed')
        return 1
    code = 0
    for f in todo:
        rc, step, text = 0, None, ''
        for st in steps:
            rc, text = runner.run(st, f)
            step = ' '.join(st)
            if rc != 0 and not (migrate and st[:3] == ['plugin', 'marketplace', 'remove']):
                break
            rc = 0
        state['folders'][f] = {'rc': rc, 'at': _stamp(now), 'step': step}
        write_state(root, state)
        if rc == 0:
            out('%s: refreshed' % f)
        else:
            code = 1
            out('FAILED %s: claude %s exited %s: %s' % (f, step, rc, (text or '').strip()[-400:]))
    return code


def ensure_export(kit, s, now=None, own=None, alive=None):
    """The export root, holding an export of the remote's main that marketplace.json names: made
    when missing, kept when current. Runs no claude command and records no account folder (an
    installer then adds the root as a marketplace). Returns (root, sha); RefreshError or Busy."""
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
        try:
            state = read_state(root)
        except StateError:
            state = None
        _folder, version = export(kit, sha, root, now)
        write_marketplace(root, name)
        if not state or state.get('sha') != sha:
            write_state(root, {'sha': sha, 'version': version, 'folder': name,
                               'exported_at': _stamp(now), 'folders': {}})
        prune(root)
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
    out('old caches (never removed by this tool; the owner removes each after the migrate is '
        'verified, docs/plugin-source.md "Migrating"):')
    for f in folders:
        out('  %s' % old_cache(f, base))


# ---------------------------------------------------------------- pm_start and pm_watch

def current_lines(kit, s):
    """pm_start lines: none when the export is at the kit's refs/remotes/<remote>/main and every
    account folder's last refresh succeeded. Reads the export state only, never an account folder:
    a folder's last refresh result stands in for what it has installed."""
    try:
        state = read_state(s.export_dir)
    except StateError as e:
        return ['could not check the pm-kit plugin: %s' % e]
    if state is None:
        return ['pm-kit plugin: no export at %s yet: %s' % (s.export_dir, REFRESH_CMD)]
    main, why = tracking_main(kit, s.remote)
    if main is None:
        return ['could not check the pm-kit plugin: %s' % why]
    lines = []
    if main and main != state.get('sha'):
        lines.append('pm-kit plugin behind kit main (exported %s, %s/%s %s): %s'
                     % (str(state.get('sha') or '?')[:12], s.remote, MAIN, main[:12], REFRESH_CMD))
    for f in sorted(state['folders']):
        r = state['folders'][f] or {}
        if r.get('rc') != 0:
            lines.append('pm-kit plugin refresh failed in %s (claude %s exited %s at %s): %s'
                         % (f, r.get('step') or '?', r.get('rc'), r.get('at') or '?', REFRESH_CMD))
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


def poll(kit, s, last, spawn=None, alive=None):
    """One pm_watch check. Returns (lines, last): an event line the first time the kit's
    refs/remotes/<remote>/main differs from the exported sha (`last` is the main last reported, so
    the same sha is not reported twice), none when current. With plugin.auto_refresh on, that first
    time also starts a refresh (`spawn(kit, root)`), unless one holds refresh.pid already."""
    main, _why = tracking_main(kit, s.remote)
    if not main:
        return [], last
    try:
        state = read_state(s.export_dir) or {}
    except StateError:
        return [], last
    if main == state.get('sha'):
        return [], None
    if main == last:
        return [], last
    line = 'pm-kit plugin behind kit main %s: %s' % (main[:12], REFRESH_CMD)
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
