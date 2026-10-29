"""Pinned tools: tools/land.py and tools/gate.py, started from the kit's main checkout, re-run
themselves from an export of pushed main (docs/gate.md "Landing", "Pinned tools").

A landing merges into the main checkout it runs from, so a tool running there would import new
modules (loaded lazily, after the merge) against old ones already loaded: a mixed generation. An
export of the kit's remote-tracking main (refs/remotes/<remote>/main) is a folder no landing
writes: the tool runs whole from one commit.

  target  the main checkout to pin from, or None: run live (already pinned, KIT_PINNED set; a
          linked worktree, whose code a landing does not merge into; an export whose marker
          names no main checkout; no remote-tracking main). A tool started from an export (the
          plugin's) pins through the main checkout its marker names, and so gets KIT_LIVE_ROOT
          like a start from that checkout: a run from the export itself would name the export
          in every command it prints
  run     export the commit (once per commit, under the export root's refresh.pid), lease it,
          run the same tool from the export with the same arguments and wait: (exit code, None),
          or (None, warning) when it cannot, and the caller runs live (fail open)

The exports live in <plugin export root>/pins/<sha12> (kitlib.pluginsource: the machine config's
plugin.export_dir, default pluginsource.default_export_dir: %LOCALAPPDATA%\\pm-kit\\plugin, on
Linux <profile>/.local/share/pm-kit/plugin), apart from the plugin's own exports,
which pluginsource.prune never confuses with them. A lease per running pinned tool,
pins/leases/<sha12>-<pid>.json {sha, pid, ctime, child_pid, child_ctime}, keeps its export: each
pin, under the lock, removes the exports no live lease holds, except the commit it runs.

The child gets KIT_PINNED=<sha> (so it, and every process it starts, runs live) and KIT_LIVE_ROOT=
<the main checkout> (kitlib.pluginsource.live_root: a printed command names the main checkout,
never an export a later prune may remove). A pin that fails open sets KIT_PINNED=live in this
process for the same reason; started from an export, run also sets KIT_LIVE_ROOT=<the main
checkout> in this process when it is unset, so the tool that then runs live from the export still
names the main checkout. Nothing here prints or exits.
"""
import json
import os
import signal
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
    has a .git file). A tool started from an export (the plugin's, say) pins through the main
    checkout its marker names (kitlib.pluginsource.export_checkout), as a start from that checkout
    would; an export whose marker names none runs live."""
    env = os.environ if environ is None else environ
    if env.get(PINNED_ENV):
        return None
    checkout = checkout_of(tool_file)
    if os.path.isdir(os.path.join(checkout, '.git')):
        return checkout
    from kitlib import pluginsource
    return pluginsource.export_checkout(checkout)


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
    if not _same(checkout, checkout_of(tool_file)):
        # Started from an export: should the pin fail open, the tool runs live from the export,
        # and what it names (printed commands) must still be the main checkout.
        from kitlib import pluginsource
        env.setdefault(pluginsource.LIVE_ROOT_ENV, checkout)
    try:
        return _run(checkout, tool_file, argv, env, wait_s)
    except KeyboardInterrupt:
        return 130, 'Ctrl+C: stopped while pinning to pushed main; the tool did not run'
    except Exception as exc:        # noqa: BLE001 - fail open: the tool still runs, live
        return _fail(env, '%s: %s' % (type(exc).__name__, exc))


def _same(a, b):
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _fail(env, why):
    env[PINNED_ENV] = LIVE
    return None, warning(why)


def _run(checkout, tool_file, argv, env, wait_s):
    from kitlib import pluginsource
    from kitlib.config import ConfigError
    from kitlib.proc import job
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
        # This run's -X utf8, -B, -I, -W ...: a private helper, so none when it is gone.
        flags = getattr(subprocess, '_args_from_interpreter_flags', list)()
        if getattr(sys.__stdout__, 'write_through', False):
            flags.append('-u')                              # ... and -u, which that leaves out
        # No console window for the tool when this process has no console (kitlib.proc.job); 0,
        # the start as it always was, when it has one. Read here, before the start.
        no_window = job.no_window_flags(0)
        # From the start the tool runs: nothing may fail open (that would run it twice). Ctrl+C
        # reaches the child too (the same console; a child does not inherit SIG_IGN,
        # tests/gate/test_pin_sigint.py) and it stops itself; this process ignores it from just
        # before Popen until the child exits, so a Ctrl+C inside Popen, or before `proc` is
        # assigned, cannot give 130 and drop the lease while the child runs on.
        code = None
        before = _ignore_ctrl_c()
        try:
            proc = subprocess.Popen([sys.executable] + flags + [tool] + list(argv),
                                    env=child_env, creationflags=no_window)
            try:
                _write_lease(lease, sha, child=proc.pid)
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - this process's lease holds it
                pass
            code = _wait(proc)
        finally:
            try:
                _restore_ctrl_c(before)
            except (Exception, KeyboardInterrupt):  # noqa: BLE001 - the tool has run: a Ctrl+C
                if code is None:        # as the handler comes back, or a restore that fails,
                    raise               # keeps its code (an error would fail open: a second run)
        return code, None
    finally:
        _remove(lease)


def _ignore_ctrl_c():
    """Set SIGINT to SIG_IGN; the handler it replaced, or None when nothing changed: off the main
    thread (signal.signal raises ValueError there, and Ctrl+C never raises there either), or a
    handler not set from Python (getsignal None), which could not be put back."""
    try:
        before = signal.getsignal(signal.SIGINT)
        if before is None:
            return None
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except ValueError:
        return None
    return before


def _restore_ctrl_c(before):
    if before is not None:
        signal.signal(signal.SIGINT, before)


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


def lease_child(tool_file, pid, environ=None):
    """A lease on the export `tool_file` runs from, held by process `pid`: a pinned tool's
    detached child (kitlib.gate.submit), which outlives the tool and so the tool's own lease. The
    path is pins/leases/<sha12>-<pid>.json, {sha, pid, ctime}; prune removes it once that process
    is gone. True when written; False when there is nothing to lease (`tool_file` is not run
    from an export of KIT_PINNED's commit, KIT_PINNED is unset or `live`, or `pid` is already
    gone). OSError when the file cannot be written."""
    from kitlib import pluginsource
    from kitlib.proc import process
    env = os.environ if environ is None else environ
    sha = env.get(PINNED_ENV)
    if not sha or sha == LIVE:
        return False
    folder = checkout_of(tool_file)
    pins = os.path.dirname(folder)
    if (os.path.normcase(os.path.basename(folder)) != os.path.normcase(pluginsource.folder_name(sha))
            or os.path.normcase(os.path.basename(pins)) != os.path.normcase(PINS)):
        return False
    ctime = process.proc_ctime(pid)
    if ctime is None:
        return False
    path = os.path.join(pins, LEASES, '%s-%d.json' % (sha[:12], pid))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump({'sha': sha, 'pid': pid, 'ctime': ctime}, fh)
    os.replace(tmp, path)
    return True


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


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _malformed(data):
    """True when the lease is not what _write_lease writes: a pid that is not a positive int a
    process id can be (past 32 bits ctypes masks it to another process's pid, which may read as
    live), a child_pid that is neither that nor absent, or a ctime that is neither an int nor
    absent (process.alive's int(ctime) would raise ValueError or TypeError)."""
    if not (_is_int(data.get('pid')) and 0 < data['pid'] <= 0xFFFFFFFF):
        return True
    child = data.get('child_pid')
    if child is not None and not (_is_int(child) and 0 < child <= 0xFFFFFFFF):
        return True
    return any(data.get(k) is not None and not _is_int(data.get(k))
               for k in ('ctime', 'child_ctime'))


def lease_live(data, alive=None):
    """True while the pinned tool's process, or the tool it started, still runs. A malformed lease
    (_malformed) is dead."""
    if _malformed(data):
        return False
    if alive is None:
        from kitlib.proc import process
        alive = process.alive
    for pid_key, ctime_key in (('pid', 'ctime'), ('child_pid', 'child_ctime')):
        pid = data.get(pid_key)
        if pid is not None and alive(pid, data.get(ctime_key)):
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
