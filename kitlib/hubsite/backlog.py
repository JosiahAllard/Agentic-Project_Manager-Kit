"""kitlib.hubsite.backlog: the hub's live backlog views, read from each repository's backlog store.

Per repository three lists: `open` (every record that is not closed, as kitlib.hubbacklog.snapshot
gives it), `completed` (closed in the last `days` days: a `closed:` line, or the file deleted) and
`parked` (the `state: parked` records with their question). Nothing is kept on disk and nothing
here writes to a repository or a store: a view is built from git on each read, and the only memory
is this process's (`Views`).

`open_read` is the one place a store is opened. A normal read opens with fetch=False, so it starts
no fetch and writes nothing. A fetch (`Views.refresh`, which the timer and a fresh read call) goes
through kitlib.backlog.store's own load: it updates the store's local ref and, as tools/backlog.py
already does, may add the store's refspec to that repository's git config. `Views` runs one fetch
round at a time, one repository after the other.
"""
import copy
import datetime
import threading

from kitlib import config as kconfig
from kitlib import hubbacklog
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Refusal
from kitlib.git import cmd as gitcmd

DAYS = hubbacklog.DAYS
NO_CHECKOUT = "no checkout of this repository on this machine"


def open_read(root, fetch=False):
    """(commit, folder) the published records of the repository at `root` are read from, the store
    `.kit.json` names: `commit` the full hash the store is at, None when it does not exist yet.
    fetch=True first brings a remote ref store into its local ref (a main store has nothing to
    fetch: its records are the main branch's). RuntimeError when the store cannot be opened or
    fetched."""
    try:
        backend = bstore.open_backend(root, kconfig.load(root), fetch=fetch)
        if fetch and isinstance(backend, bstore.RefBackend) and backend.remote:
            backend.load(None)
    except (kconfig.ConfigError, Refusal) as exc:
        raise RuntimeError("backlog store: %s" % exc)
    rev, folder = backend.landed(hubbacklog.main_branch(root, hubbacklog.kit_json(root)))
    r = gitcmd.subprocess_run(["git", "-C", root, "rev-parse", "--verify", "-q", rev + "^{commit}"],
                              capture_output=True, timeout=hubbacklog.TIMEOUT)
    commit = r.stdout.decode("ascii", "replace").strip() if r.returncode == 0 else ""
    return (commit or None), folder


def read_at(name, root, commit, folder, since, days=DAYS):
    """One repository's view from the store at `commit`: {"repo", "days", "open", "completed",
    "parked"}. `since` is the oldest closing date (YYYY-MM-DD) `completed` keeps."""
    out = {"repo": name, "days": days, "open": [], "completed": [], "parked": []}
    if commit is None:
        return out
    records = hubbacklog.read_store(root, commit, folder)
    for rid, text in sorted(records.items()):
        rec = hubbacklog.parse(text)
        if "id" not in rec:
            continue  # not a record (an index or readme carries no id line)
        closed = hubbacklog.first(rec, "closed")
        if closed:
            if closed >= since:
                out["completed"].append({"id": rid, "t": hubbacklog.title(rec), "d": closed})
            continue
        state = hubbacklog.first(rec, "state", "open")
        out["open"].append({"id": rid, "t": hubbacklog.title(rec), "p": hubbacklog.prio(rec), "s": state})
        if state == "parked":
            out["parked"].append({"id": rid, "t": hubbacklog.title(rec), "q": hubbacklog.first(rec, "q"),
                                  "parked_at": hubbacklog.first(rec, "parked_at"),
                                  "session": hubbacklog.first(rec, "session"),
                                  "branch": hubbacklog.first(rec, "pulled_by")})
    for rid, when, t in hubbacklog.deleted(root, commit, folder, since):
        if rid not in records:  # deleted and later re-added: still in the store
            out["completed"].append({"id": rid, "t": t, "d": when})
    out["completed"].sort(key=lambda c: c["d"], reverse=True)
    return out


def repo_view(name, root, now, opener=open_read, fetch=False, days=DAYS, cache=None):
    """One repository's view, plus "error": None, or why it could not be read (the lists are then
    empty). `cache` ({name: (key, view)}, the caller's) spares the read while the store is at the
    same commit; the store is still opened every time, so a moved store is always seen."""
    view = {"repo": name, "days": days, "open": [], "completed": [], "parked": [], "error": None}
    if not root:
        view["error"] = NO_CHECKOUT
        return view
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        commit, folder = opener(root, fetch)
        key = (root, commit, folder, since, days)
        held = (cache or {}).get(name)
        if held and held[0] == key:
            got = held[1]
        else:
            got = read_at(name, root, commit, folder, since, days)
            if cache is not None:
                cache[name] = (key, got)
    except Exception as exc:  # one repository's failure must not hide the others
        view["error"] = str(exc) or exc.__class__.__name__
        return view
    view.update(copy.deepcopy(got))
    return view


def views(repos, now, opener=open_read, fetch=False, days=DAYS, cache=None):
    """{"at": now as ISO, "repos": [repo_view, ...]} for `repos`, (name, root) pairs in the
    caller's order (root None: no checkout here). `now` is an aware datetime."""
    return {"at": now.isoformat(),
            "repos": [repo_view(name, root, now, opener, fetch, days, cache) for name, root in repos]}


def repo_list(names, pm_root, paths=None):
    """[(name, root or None)] for repository names, each found as kitlib.hubbacklog.repo_root does."""
    return [(n, hubbacklog.repo_root(n, pm_root, paths)) for n in names]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


class Views(object):
    """The views every hub client shares, with their fetches.

    `repos`: (name, root) pairs, or a callable giving them (asked on every read, so a new
    repository appears on its own). `refresh_s`: seconds between the timer's fetch rounds (None or
    0: no timer). `clock`: gives an aware datetime. `opener`: `open_read`, or a stand-in with its
    signature.

    `get()` reads with no fetch. `refresh()` fetches every repository, one at a time, then reads;
    `get(fresh=True)` is the same. Only one round runs at a time: a call that waited through a
    round started after it arrived takes that round's result instead of fetching again. `start()`
    runs `refresh()` every `refresh_s` on a daemon thread until `stop()`.

    Each repository's entry carries, besides `repo_view`'s keys, "fetched" (ISO time of its last
    good fetch in this process, or None) and "fetch_error" (why its last fetch failed, or None).
    """

    def __init__(self, repos, refresh_s=None, clock=utc_now, opener=open_read, days=DAYS):
        self._repos = repos
        self.refresh_s = refresh_s
        self.clock = clock
        self.opener = opener
        self.days = days
        self.timer_error = None        # why the timer's last round raised, None when it did not
        self._cache = {}
        self._fetched = {}
        self._fetch_error = {}
        self._fetching = threading.Lock()
        self._rounds = 0
        self._stop = threading.Event()
        self._thread = None

    def repos(self):
        got = self._repos() if callable(self._repos) else self._repos
        return [(name, root) for name, root in got]

    def get(self, fresh=False):
        """The view now: {"at", "repos": [...]}. The caller owns what it gets."""
        if fresh:
            return self.refresh()
        out = views(self.repos(), self.clock(), self.opener, False, self.days, self._cache)
        for v in out["repos"]:
            v["fetched"] = self._fetched.get(v["repo"])
            v["fetch_error"] = self._fetch_error.get(v["repo"])
        return out

    def refresh(self):
        """Fetch every repository's store, one at a time, then `get()`."""
        seen = self._rounds
        with self._fetching:
            if self._rounds == seen:       # else: a round began after this call arrived
                self._rounds += 1
                for name, root in self.repos():
                    if not root:
                        continue
                    try:
                        self.opener(root, True)
                    except Exception as exc:
                        self._fetch_error[name] = str(exc) or exc.__class__.__name__
                    else:
                        self._fetch_error.pop(name, None)
                        self._fetched[name] = self.clock().isoformat()
        return self.get()

    def start(self):
        """Start the timer; False when there is none to start (no interval, or already running)."""
        if self._thread is not None or not self.refresh_s or self.refresh_s <= 0:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hub-backlog-refresh", daemon=True)
        self._thread.start()
        return True

    def _run(self):
        while not self._stop.wait(self.refresh_s):
            try:
                self.refresh()
                self.timer_error = None
            except Exception as exc:
                self.timer_error = str(exc) or exc.__class__.__name__

    def stop(self, timeout=None):
        """End the timer and wait for its thread (a round in flight finishes first)."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)
