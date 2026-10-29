"""The lock release pre-check (docs/lock-release.md). tools/lock_release_check.py is
the entry point.

The lock runs from the kit's release checkout like every tool. A release that changes the lock
tool, moving that checkout to a commit whose lock code differs, is refused while any run is live:
a live run's wrapper keeps the code it started with, and the next command it starts would mix
old and new. The caller's own run (its process or an ancestor holds one) counts too, with its
own reason: the check must run outside any lock run.

`check(kit, commit, store)` -> Outcome(code, lines, command). It reads; it never moves a ref,
touches the work tree or writes to the store. On OK, `command` is the git command that makes the
release; run it at once, since a run that starts after the check is not seen.

  OK 0       nothing to release; or the release changes no LOCK_PATHS file; or it does and no
             run is live (runs whose process is surely gone are listed as ignored)
  REFUSED 1  it changes a LOCK_PATHS file and a run is live (the caller's own included), the
             store folder is missing, or git cannot say what changes
  USAGE 2    no checkout there, or commit names no commit
  ERROR 3    an unexpected failure (reading the store, asking Windows about a process): not
             checked, so no release

What the release brings in is the diff from the checkout's HEAD to commit on a fast-forward, or
from their merge base to commit when it takes a merge. Runs are the store's active/ records and
queue.json entries (live_runs); a background run has both. A record that cannot be read, or that
names no owning process, counts as live.
"""
import collections
import json
import os
import time

import kitlib.platform
from kitlib.git import cmd, refs
from kitlib.lock.store import ID_RE, Store, read_json, wrapper_state
from kitlib.platform.linux import process as procfs
from kitlib.proc import process

OK, REFUSED, USAGE, ERROR = 0, 1, 2, 3

# What the lock tool runs: its entry point and every kitlib package it imports (at any depth;
# tests/lock/test_release.py walks the imports and fails when one falls outside). A name ending
# in "/" is a folder.
LOCK_PATHS = ("tools/machine_lock.py", "kitlib/__init__.py", "kitlib/lock/", "kitlib/proc/",
              "kitlib/config/", "kitlib/git/",
              # kitlib/config/policy.py reads agent names through kitlib.agentname, whose prefix
              # is the plugin name in kitlib/lockmcp/__init__.py
              "kitlib/agentname.py", "kitlib/lockmcp/__init__.py",
              # the agent process images and session variable come from the host registry
              "kitlib/host/", "hosts.json",
              # the machine config reader checks the PM-repo pointer's kit-pm-data.json record
              "kitlib/pmdata/__init__.py",
              # kitlib/config/policy.py takes names_of (a role's old and new names) from
              # kitlib/content/__init__.py, whose render function imports kitlib/roster/__init__.py
              # (never run by the lock tool; listed because the import walk counts it)
              "kitlib/content/__init__.py", "kitlib/roster/__init__.py",
              # the running platform's name (registry.process_names) and the Windows DLL loader
              # kitlib/proc binds through
              "kitlib/platform/__init__.py", "kitlib/platform/dll.py",
              # kitlib/lock/layout.py reads Linux hardware through the Linux backend and its seam
              "kitlib/platform/api.py", "kitlib/platform/linux/",
              # machine_lock.py run may send the run to a load node (kitlib.loadnode.dispatch)
              "kitlib/loadnode/")

MISSING = "store folder (missing)"

Outcome = collections.namedtuple("Outcome", "code lines command")


def is_lock_path(rel):
    """Is the repo-relative path `rel` part of the lock tool (LOCK_PATHS)?"""
    rel = rel.replace("\\", "/")
    return any(rel.startswith(p) if p.endswith("/") else rel == p for p in LOCK_PATHS)


def default_store():
    """The machine's store (read-only here)."""
    return Store()


def callers_processes():
    """{(pid, ctime)} of this process and its ancestors: a run one of them owns is the caller's."""
    return {(os.getpid(), process.own_ctime())} | process.own_ancestors()


def error_outcome(err):
    """ERROR for an unexpected failure: nothing was checked."""
    return Outcome(ERROR, ["ERROR: the check failed (%s: %s); nothing was checked, so do not "
                           "release." % (type(err).__name__, err)], None)


def _other_ns(d):
    """Does d name a pid namespace (`pidns`, Linux) other than this process's? Its pid is then
    not one this process can check (store.record_state)."""
    ns = d.get("pidns")
    return ns is not None and ns != procfs.pidns()


def _owner_live(d, alive, mutex_gone=None):
    """Is the run d (a record or a queue entry) live? False only when its process is surely
    gone: alive(pid, ctime) says False; or alive cannot tell (None, or True for a record with no
    creation time: a pid alone is not proof of life) and mutex_gone(id) says its run mutex is
    surely gone (store.run_mutex_held False). On Linux alive's False is not enough (a checker in
    its own pid namespace sees no wrapper's pid, as store.run_dead), and neither is any answer
    for a d of another pid namespace: the run lock decides."""
    if not isinstance(d, dict):
        return True
    pid, ctime = d.get("pid"), d.get("ctime")
    if type(pid) is not int or pid <= 0 or (ctime is not None and type(ctime) is not int):
        return True
    try:
        state = alive(pid, ctime)
        if kitlib.platform.NAME == "linux" and (state is False or _other_ns(d)):
            state = None
    except (OSError, ValueError, TypeError, OverflowError):
        return True
    if state is not None and (ctime is not None or not state):
        return bool(state)
    rid = d.get("id")
    if mutex_gone is None or not isinstance(rid, str) or not ID_RE.match(rid):
        return True
    try:
        return not mutex_gone(rid)
    except (OSError, ValueError, TypeError):
        return True


def _is_mine(d, mine):
    """Does the run d belong to one of the processes in mine ({(pid, ctime)})? Never one of
    another pid namespace (Linux): its pid names another process here."""
    if not isinstance(d, dict):
        return False
    if kitlib.platform.NAME == "linux" and _other_ns(d):
        return False
    pid, ctime = d.get("pid"), d.get("ctime")
    return any(pid == p and (ctime is None or ctime == c) for p, c in mine)


def _queue_entries(store):
    """The raw entries of queue.json ([] when there is none), or None when it cannot be read for
    settings.queue_read_s. Malformed entries are kept: the caller counts them as live."""
    path = store.path("queue.json")
    end = time.monotonic() + store.settings.queue_read_s
    while True:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if not isinstance(d, dict) or not isinstance(d.get("entries", []), list):
                raise ValueError("not a queue")
            return list(d.get("entries") or [])
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            if time.monotonic() >= end:
                return None
            time.sleep(0.1)


def live_runs(store, alive=None, mine=None):
    """(live, gone, own): the store's runs as 'active/<file>' and 'queue.json:<id>' ('?' for an
    entry with no id). Gone means the run's process is surely gone; anything unreadable is live,
    and a missing store folder is the one live entry MISSING. own: the live runs whose process is
    in mine ({(pid, ctime)}). Writes nothing. alive(pid, ctime): True, False, or None when it
    cannot tell (store.wrapper_state, the default); then the run's mutex decides
    (store.run_mutex_held), as store.run_live does."""
    alive = alive or wrapper_state
    mine = mine or set()
    if not os.path.isdir(store.path()):
        return [MISSING], [], []
    live, gone, own = [], [], []

    def sort(name, d):
        if not _owner_live(d, alive, lambda rid: store.run_mutex_held(rid) is False):
            gone.append(name)
            return
        live.append(name)
        if _is_mine(d, mine):
            own.append(name)

    try:
        names = sorted(os.listdir(store.path("active")))
    except FileNotFoundError:
        names = []
    except OSError:
        names = []
        live.append("active/ (unreadable)")
    for n in names:
        if n.endswith(".json"):
            sort("active/" + n, read_json(store.path("active", n)))
    entries = _queue_entries(store)
    if entries is None:
        live.append("queue.json (unreadable)")
        entries = []
    for e in entries:
        rid = e.get("id") if isinstance(e, dict) else None
        sort("queue.json:%s" % (rid if isinstance(rid, str) and rid else "?"), e)
    return live, gone, own


def release_plan(kit, target):
    """(base, merge, None): the release of target brings in base..target, by a merge when merge
    is True, else a fast-forward; (None, None, None) when HEAD already holds target; or
    (None, None, why)."""
    head, why = refs.head(kit)
    if head is None:
        return None, None, why
    held, why = refs.is_ancestor(target, head, kit)
    if held is None:
        return None, None, why
    if held:
        return None, None, None
    ff, why = refs.is_ancestor(head, target, kit)
    if ff is None:
        return None, None, why
    if ff:
        return head, False, None
    base, why = refs.merge_base(head, target, kit)
    if base is None:
        return None, None, why
    if not base:
        return None, None, "%s and HEAD share no history" % target
    return base, True, None


def changed_paths(kit, base, target):
    """([repo-relative path], None) changed from base to target (renames as both names), or
    (None, why)."""
    out, why = cmd.run_text(["diff", "--name-only", "--no-renames", "-z", base, target, "--"], kit,
                            strip=False)
    if out is None:
        return None, why
    return [p for p in out.split("\0") if p.strip()], None


def check(kit, commit, store=None, alive=None, mine=None):
    """Outcome of releasing commit into the checkout kit (module docstring). mine: the caller's
    processes ({(pid, ctime)}; default callers_processes())."""
    try:
        return _check(kit, commit, store, alive, mine)
    except Exception as err:                    # noqa: BLE001 - ERROR, never OK or STOP
        return error_outcome(err)


def _check(kit, commit, store, alive, mine):
    kit = os.path.abspath(kit)
    if not os.path.isdir(kit):
        return Outcome(USAGE, ["USAGE: no checkout at %s" % kit], None)
    target, why = refs.resolve(commit, kit)
    if target is None:
        return Outcome(USAGE, ["USAGE: %s" % why], None)
    base, merge, why = release_plan(kit, target)
    if why:
        return Outcome(REFUSED, ["STOP: cannot tell what releasing %s would change: %s"
                                 % (target, why)], None)
    if base is None:
        return Outcome(OK, ["%s is already in the release checkout %s: nothing to release."
                            % (target, kit)], None)
    command = 'git -C "%s" merge %s %s' % (kit, "--no-ff" if merge else "--ff-only", target)
    changed, why = changed_paths(kit, base, target)
    if changed is None:
        return Outcome(REFUSED, ["STOP: cannot list what releasing %s would change: %s"
                                 % (target, why)], None)
    lock = [p for p in changed if is_lock_path(p)]
    how = "a merge" if merge else "a fast-forward"
    if not lock:
        return Outcome(OK, ["The release (%s) changes no lock tool file; runs may stay live." % how,
                            "Release: %s" % command], command)
    store = store or default_store()
    live, gone, own = live_runs(store, alive, callers_processes() if mine is None else mine)
    lines = ["The release (%s) changes %d lock tool file(s):" % (how, len(lock))]
    lines += ["  %s" % p for p in lock]
    lines += ["ignored (its process is gone): %s" % g for g in gone]
    if live:
        if MISSING in live:
            head = ["STOP: the lock store %s does not exist; cannot tell whether a run is live."
                    % store.root]
        elif own:
            head = ["STOP: this check runs inside a lock run (your own process or one of its "
                    "ancestors): %s" % ", ".join(own), "Run it outside any lock run."]
        else:
            head = []
        head.append("STOP: the release changes the lock tool and %d run(s) are live."
                    % len(live))
        lines = head + lines + ["live: %s" % r for r in live]
        lines.append("Release once no run is live; run this check again first.")
        return Outcome(REFUSED, lines, None)
    lines += ["No run is live. Release at once (a run that starts first is not seen): %s"
              % command]
    return Outcome(OK, lines, command)
