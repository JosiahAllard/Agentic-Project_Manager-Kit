"""Pinned tools: tools/land.py and tools/gate.py, started from the kit's main checkout, re-run
themselves from an export of pushed main (docs/gate.md "Landing", "Pinned tools").

A landing merges into the main checkout it runs from, so a tool running there would import new
modules (loaded lazily, after the merge) against old ones already loaded: a mixed generation. An
export of the kit's remote-tracking main (refs/remotes/<remote>/main) is a folder no landing
writes: the tool runs whole from one commit.

  target  the main checkout to pin from, or None: run live (already pinned, KIT_PINNED set; a
          linked worktree or an export, whose code a landing does not merge into; no
          remote-tracking main)
  run     export the commit (once per commit, under the export root's refresh.pid), lease it,
          run the same tool from the export with the same arguments and wait: (exit code, None),
          or (None, warning) when it cannot, and the caller runs live (fail open)

The exports live in <plugin export root>/pins/<sha12> (kitlib.pluginsource: the machine config's
plugin.export_dir, default %LOCALAPPDATA%\\pm-kit\\plugin), apart from the plugin's own exports,
which pluginsource.prune never confuses with them. A lease per running pinned tool,
pins/leases/<sha12>-<pid>.json {sha, pid, ctime, child_pid, child_ctime}, keeps its export: each
pin, under the lock, removes the exports no live lease holds, except the commit it runs.

The child gets KIT_PINNED=<sha> (so it, and every process it starts, runs live) and KIT_LIVE_ROOT=
<the main checkout> (kitlib.pluginsource.live_root: a printed command names the main checkout,
never an export a later prune may remove). A pin that fails open sets KIT_PINNED=live in this
process for the same reason. Nothing here prints or exits.
"""
import json
import os
import subprocess
import sys
import time

PINNED_ENV = 'KIT_PINNED'
LIVE = 'live'                   # KIT_PINNED when a pin failed open and the tool runs live
PINS = 'pins'
LEASES = 'leases'
MADE_BY = 'kitlib.gate.pin'
LOCK_WAIT_S = 60.0
LOCK_POLL_S = 0.1


def checkout_of(tool_file):
    """The checkout a tools/<name>.py is in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(tool_file)))


def target(tool_file, environ=None):
    """The checkout to pin `tool_file` from, or None to run live: KIT_PINNED is set (already
    pinned, or a pin that failed open), or the checkout is not a main checkout (a linked worktree
    has a .git file, an export has none)."""
    env = os.environ if environ is None else environ
    if env.get(PINNED_ENV):
        return None
    checkout = checkout_of(tool_file)
    if not os.path.isdir(os.path.join(checkout, '.git')):
        return None
    return checkout


def warning(why):
    return 'WARNING: pin: running the live checkout\'s code: %s' % ' '.join(str(why).split())


def run(tool_file, argv, environ=None, wait_s=LOCK_WAIT_S):
    """Run `tool_file` from an export of pushed main with `argv`, when target() names a checkout:
    (the tool's exit code, None). (None, None) when there is nothing to pin (target() None, or no
    remote-tracking main); (None, warning) when the pin failed, KIT_PINNED=live set in
    `environ` (default os.environ) so nothing below tries again."""
    env = os.environ if environ is None else environ
    checkout = target(tool_file, env)
    if checkout is None:
        return None, None
    try:
        return _run(checkout, tool_file, argv, env, wait_s)
    except KeyboardInterrupt:
        return 130, 'Ctrl+C: stopped while pinning to pushed main; the tool did not run'
    except Exception as exc:        # noqa: BLE001 - fail open: the tool still runs, live
        return _fail(env, '%s: %s' % (type(exc).__name__, exc))


def _fail(env, why):
    env[PINNED_ENV] = LIVE
    return None, warning(why)


def _run(checkout, tool_file, argv, env, wait_s):
    from kitlib import pluginsource
    from kitlib.config import ConfigError
    try:
        settings = pluginsource.load(environ=env, kit=checkout)
    except (pluginsource.RefreshError, ConfigError) as exc:
        return _fail(env, 'the plugin export root: %s' % exc)
    sha, why = pluginsource.tracking_main(checkout, settings.remote)
    if sha is None:
        return _fail(env, 'cannot read %s/%s: %s' % (settings.remote, pluginsource.MAIN, why))
    if not sha:
        env[PINNED_ENV] = LIVE
        return None, None           # nothing pushed to pin to: run live, nothing to warn about
    root = os.path.join(settings.export_dir, PINS)
    folder, lease, why = ensure(checkout, sha, root, wait_s)
    if folder is None:
        return _fail(env, why)
    try:
        tool = os.path.join(folder, 'tools', os.path.basename(tool_file))
        if not os.path.isfile(tool):
            return _fail(env, 'the export of %s has no tools/%s' % (sha[:12],
                                                                    os.path.basename(tool_file)))
        child_env = dict(env)
        child_env[PINNED_ENV] = sha
        child_env[pluginsource.LIVE_ROOT_ENV] = checkout
        proc = subprocess.Popen([sys.executable, tool] + list(argv), env=child_env)
        # From here the tool runs: nothing may fail open (that would run it twice).
        try:
            _write_lease(lease, sha, child=proc.pid)
        except Exception:               # noqa: BLE001 - this process's lease holds the export
            pass
        return _wait(proc), None
    finally:
        _remove(lease)


def _wait(proc):
    """The child's exit code. Ctrl+C reaches the child too (the same console): it stops itself
    and says so; this process only goes on waiting for it."""
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:
            continue


def ensure(checkout, sha, root, wait_s=LOCK_WAIT_S, clock=None, sleep=None):
    """Under <root>/refresh.pid: the export <root>/<sha12> of `sha` (made when missing), a lease on
    it for this process, and the exports no live lease holds removed. (folder, lease path, None),
    or (None, None, why)."""
    from kitlib import pluginsource
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    os.makedirs(root, exist_ok=True)
    lock = pluginsource.RefreshLock(root)
    deadline = clock() + wait_s
    while True:
        try:
            lock.acquire()
            break
        except pluginsource.Busy as exc:
            if clock() >= deadline:
                return None, None, 'the pin export lock stayed busy for %d s (%s)' % (wait_s, exc)
            sleep(LOCK_POLL_S)
    try:
        folder, _version = pluginsource.export(checkout, sha, root, made_by=MADE_BY)
        lease = _lease_path(root, sha)
        _write_lease(lease, sha)
        prune(root, keep=pluginsource.folder_name(sha))
    except (pluginsource.RefreshError, OSError) as exc:
        return None, None, 'cannot export %s: %s' % (sha[:12], exc)
    finally:
        lock.release()
    return folder, lease, None


def _lease_path(root, sha):
    return os.path.join(root, LEASES, '%s-%d.json' % (sha[:12], os.getpid()))


def _write_lease(path, sha, child=None):
    from kitlib.proc import process
    data = {'sha': sha, 'pid': os.getpid(), 'ctime': process.own_ctime()}
    if child is not None:
        data.update(child_pid=child, child_ctime=process.proc_ctime(child))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def read_lease(path):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def lease_live(data, alive=None):
    """True while the pinned tool's process, or the tool it started, still runs."""
    if alive is None:
        from kitlib.proc import process
        alive = process.alive
    for pid_key, ctime_key in (('pid', 'ctime'), ('child_pid', 'child_ctime')):
        pid = data.get(pid_key)
        if isinstance(pid, int) and pid > 0 and alive(pid, data.get(ctime_key)):
            return True
    return False


def prune(root, keep=None, alive=None, clock=None):
    """Under the lock: remove the leases whose processes are gone (and lease files that cannot be
    read, or a lease's .tmp, once older than pluginsource.LOCK_GRACE_S: younger, the <sha12> its
    name starts with stays held), then every <sha12>
    export holding the marker that no live lease holds, except `keep`, and every <sha12>.tmp left
    by a run that stopped. Returns [removed path]."""
    from kitlib import pluginsource
    clock = clock or time.time
    held, removed = set(), []
    leases = os.path.join(root, LEASES)
    try:
        names = os.listdir(leases)
    except OSError:
        names = []
    for n in names:
        full = os.path.join(leases, n)
        data = read_lease(full) if n.endswith('.json') else None
        if data is not None and isinstance(data.get('sha'), str):
            if lease_live(data, alive):
                held.add(pluginsource.folder_name(data['sha']))
                continue
        else:
            try:
                if clock() - os.path.getmtime(full) <= pluginsource.LOCK_GRACE_S:
                    held.add(n[:12])    # being written: its name says which export it holds
                    continue
            except OSError:
                held.add(n[:12])
                continue
        _remove(full)
    try:
        names = os.listdir(root)
    except OSError:
        return removed
    for n in sorted(names):
        full = os.path.join(root, n)
        if not os.path.isdir(full) or pluginsource._is_link(full):
            continue
        if pluginsource._TMP.match(n):
            stale = True                # the lock is held: no export is being written
        elif pluginsource._SHA12.match(n):
            stale = (n != keep and n not in held
                     and pluginsource.read_marker(full) is not None)
        else:
            stale = False
        if stale:
            try:
                pluginsource._rmtree(full)
                removed.append(full)
            except OSError:
                pass                    # a file still open: the next pin tries again
    return removed
