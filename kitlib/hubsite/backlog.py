"""kitlib.hubsite.backlog: the hub's live backlog views, read from each repository's backlog store.

Per repository three lists: `open` (every record that is not closed, as kitlib.hubbacklog.snapshot
gives it), `completed` (closed in the last `days` days: a `closed:` line, or the file deleted) and
`parked` (the open items whose state is `parked`, with their question: `parked_item`). Nothing is kept on disk and nothing
here writes to a repository or a store: a view is built from git, and the only memory is this
process's (`Views`).

`open_read` is the one place a store is opened. A normal read opens with fetch=False, so it starts
no fetch and writes nothing. A fetch (`Views.refresh`, which the timer and a fresh read call) goes
through kitlib.backlog.store's own load: it updates the store's local ref and, as tools/backlog.py
already does, may add the store's refspec to that repository's git config. `Views` runs one fetch
round at a time, one repository after the other.

Against the documents kitlib.hubbacklog posted: there is no size cap here, so no `cut` count (a
view is an answer, not a stored document), and no `posted_at` (the answer's `at` is its time).

No error text in a view names a folder of this machine or a remote's address: `say` puts the
repository's name where its folder stood, and a failed fetch is told in fixed words (`fetch_say`).
"""
import copy
import datetime
import re
import threading

from kitlib import config as kconfig
from kitlib import hubbacklog
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Refusal
from kitlib.git import cmd as gitcmd
from kitlib.git import refs

DAYS = hubbacklog.DAYS
REUSE_S = 5         # seconds `Views.get()` answers from what it read last, starting no git
NO_CHECKOUT = "no checkout of this repository on this machine"
NO_REPOS = "the repository list could not be read"
FETCH_FAILED = "the fetch failed"
_MAIN = object()    # handed to a backend's landed(): a main store answers with it
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+|\S+@[\w.-]+:\S+")
_EXITED = re.compile(r"\bexited (-?\d+)\b")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")


def say(exc, name, root):
    """Why something failed, as a view may show it: `exc`'s text with the repository's `name` in
    place of its folder `root` (either slash, any case) and `<address>` in place of a URL."""
    text = str(exc) or exc.__class__.__name__
    if root:
        parts = [re.escape(p) for p in re.split(r"[\\/]+", root) if p]
        if parts:
            folder = (r"[\\/]+" if root[0] in "\\/" else "") + r"[\\/]+".join(parts)
            text = re.sub(folder, lambda m: name, text, flags=re.IGNORECASE)
    return _URL.sub("<address>", text)


def fetch_say(exc):
    """A failed fetch in fixed words: FETCH_FAILED, with git's exit code when the failure names
    one. Never git's own words: they can carry the remote's address."""
    m = _EXITED.search(str(exc))
    return "%s (git exited %s)" % (FETCH_FAILED, m.group(1)) if m else FETCH_FAILED


def main_commit(root):
    """The commit of the main branch of the repository at `root`: the branch `.kit.json` names
    (repo.main_branch), else `main`, else `master`. RuntimeError when it is not there (a misnamed
    main branch is a failure, never an empty store) or git cannot tell."""
    named = (hubbacklog.kit_json(root).get("repo") or {}).get("main_branch")
    for branch in ([named] if named else ["main", "master"]):
        commit, why = refs.ref_commit("refs/heads/%s" % branch, root)
        if commit is None:
            raise RuntimeError("cannot read the main branch `%s`: %s" % (branch, why))
        if commit:
            return commit
    if named:
        raise RuntimeError("the main branch `%s` (.kit.json repo.main_branch) does not exist" % named)
    raise RuntimeError("no main branch: neither `main` nor `master` exists")


def open_read(root, fetch=False):
    """(commit, folder, fetched) the published records of the repository at `root` are read from,
    the store `.kit.json` names. `commit`: the full hash the store is at; None only when git
    answered that the store's ref does not exist (no store yet). `fetched`: True when a fetch ran.
    fetch=True first brings a remote ref store into its local ref; a main store, and a ref store
    in a repository with no remote, have nothing to fetch.

    RuntimeError when the store cannot be opened or fetched, and whenever git gives no answer (it
    could not start, timed out, was ended, or refused the repository): only "no such ref" reads as
    empty (kitlib.git.refs ref_commit). Every git here runs through kitlib.git.cmd.call: scrubbed
    environment, a time limit, its own job."""
    fetched = False
    try:
        backend = bstore.open_backend(root, kconfig.load(root), fetch=fetch)
        if fetch and isinstance(backend, bstore.RefBackend) and backend.remote:
            backend.load(None)
            fetched = True
    except (kconfig.ConfigError, Refusal) as exc:
        raise RuntimeError("backlog store: %s" % exc)
    rev, folder = backend.landed(_MAIN)
    if rev is _MAIN:
        return main_commit(root), folder, fetched
    if rev.startswith("refs/"):
        commit, why = refs.ref_commit(rev, root)
    else:
        commit, why = refs.resolve(rev, root, known=True)
    if commit is None:
        raise RuntimeError("cannot read the backlog store at %s: %s" % (rev, why))
    return (commit or None), folder, fetched


def window(now, days=DAYS):
    """The first day `completed` keeps, YYYY-MM-DD: the UTC date of `now` less `days` days. The
    window opens at that day's midnight UTC, whatever the time of day of the read."""
    return (now.astimezone(datetime.timezone.utc).date() - datetime.timedelta(days=days)).isoformat()


def parked_item(rec, root, commit, path):
    """What a parked record (`rec`, kitlib.hubbacklog.parse's; its file `path` at `commit`) adds to
    its open item. Every key is always there, None when the record does not hold it:

    - "q": the question it is parked on (the record's `q:` line).
    - "parked_at", an ISO timestamp, and "parked_at_from": "status" when it is the record's own
      `parked_at:` line (the day `set-status parked` wrote, given as that day's midnight UTC; the
      record keeps a day, not a time), else "updated": the committer date of the last commit at or
      before `commit` that changed the record's file (one `git log -1` through kitlib.git.cmd,
      with its time limit; RuntimeError when git gives no answer). Both None when neither is known.
    - "branch" (the record's `pulled_by:`) and "session" (its `session:`). Parking gives up the
      pull, so a record parked by tools/backlog.py holds no branch."""
    day = hubbacklog.first(rec, "parked_at")
    if _DAY.match(day):
        when, how = day + "T00:00:00+00:00", "status"
    else:
        when, why = gitcmd.run_text(["log", "-1", "--format=%cI", commit, "--", path], root)
        if when is None:
            raise RuntimeError("cannot read when %s last changed: %s" % (path, why))
        when, how = (when, "updated") if when else (None, None)
    return {"q": hubbacklog.first(rec, "q") or None, "parked_at": when, "parked_at_from": how,
            "branch": hubbacklog.first(rec, "pulled_by") or None,
            "session": hubbacklog.first(rec, "session") or None}


def read_at(name, root, commit, folder, since, days=DAYS):
    """One repository's view from the store at `commit`: {"repo", "days", "open", "completed",
    "parked"}. `since` (YYYY-MM-DD, `window`) is the first closing day `completed` keeps: a
    `closed:` line of that day or later, or a file deleted at or after that day's midnight UTC
    (git is given the full timestamp; a bare date would mean "that day at this time of day").

    An open item whose state is `parked` also carries `parked_item`'s keys, and "parked" lists
    those same items: the open items with "s" == "parked"."""
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
        item = {"id": rid, "t": hubbacklog.title(rec), "p": hubbacklog.prio(rec), "s": state}
        out["open"].append(item)
        if state == "parked":
            item.update(parked_item(rec, root, commit, "%s/%s.md" % (folder, rid)))
            out["parked"].append(dict(item))
    for rid, when, t in hubbacklog.deleted(root, commit, folder, since + "T00:00:00+00:00"):
        if rid not in records:  # deleted and later re-added: still in the store
            out["completed"].append({"id": rid, "t": t, "d": when})
    out["completed"].sort(key=lambda c: c["d"], reverse=True)
    return out


def repo_view(name, root, now, opener=open_read, fetch=False, days=DAYS, cache=None, opened=None):
    """One repository's view: `read_at`'s keys plus "error" (None, or why it could not be read)
    and "stale". A failed read keeps the lists of the last good read `cache` holds for this
    repository, with "stale": True; with none to keep the lists are empty.

    `cache` ({name: (key, view)}, the caller's) spares the read while the store is at the same
    commit and the window on the same day. `opened`: what `opener` already answered for this
    repository (a fetch round's open), so it is not opened again."""
    view = {"repo": name, "days": days, "open": [], "completed": [], "parked": [], "error": None,
            "stale": False}
    if not root:
        view["error"] = NO_CHECKOUT
        return view
    since = window(now, days)
    held = (cache or {}).get(name)
    try:
        commit, folder = (opened if opened is not None else opener(root, fetch))[:2]
        key = (root, commit, folder, since, days)
        if held and held[0] == key:
            got = held[1]
        else:
            got = read_at(name, root, commit, folder, since, days)
            if cache is not None:
                cache[name] = (key, got)
    except Exception as exc:  # one repository's failure must not hide the others
        if held and held[0][0] == root:
            view.update(copy.deepcopy(held[1]))
            view["stale"] = True
        view["error"] = say(exc, name, root)
        return view
    view.update(copy.deepcopy(got))
    return view


def views(repos, now, opener=open_read, fetch=False, days=DAYS, cache=None):
    """{"at": now as ISO, "repos": [repo_view, ...], "error": None} for `repos`, (name, root)
    pairs in the caller's order (root None: no checkout here). `now` is an aware datetime."""
    return {"at": now.isoformat(), "error": None,
            "repos": [repo_view(name, root, now, opener, fetch, days, cache) for name, root in repos]}


def repo_list(names, pm_root, paths=None):
    """[(name, root or None)] for repository names, each found as kitlib.hubbacklog.repo_root does."""
    return [(n, hubbacklog.repo_root(n, pm_root, paths)) for n in names]


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def event_wait(event, seconds):
    """True when `event` was set within `seconds`: the timer's pause between two rounds."""
    return event.wait(seconds)


class Views(object):
    """The views every hub client shares, with their fetches.

    `repos`: (name, root) pairs, or a callable giving them (asked on every read, so a new
    repository appears on its own). `refresh_s`: seconds between the timer's rounds (None or 0:
    no timer). `clock`: gives an aware datetime. `opener`: `open_read`, or a stand-in with its
    signature and answer. `reuse_s`: seconds a repository's last read answers `get()` with no git
    started (0: every `get()` opens the store again). `wait`: `event_wait`, or a stand-in (tests).

    `get()` never fetches. `refresh()` runs a round: each repository in turn is fetched and read
    again (the local copy when its fetch failed); `get(fresh=True)` is the same. Only one round
    runs at a time: a call that waited through a round started after it arrived takes that round's
    result instead of fetching again. `start()` runs a round at once and then every `refresh_s`,
    on a daemon thread, until `stop()`.

    The answer is {"at", "repos", "error"}: "error" None, or NO_REPOS with the list's failure when
    the `repos` callable raised ("repos" is then the last good answer's). Each repository's entry
    carries, besides `repo_view`'s keys, "fetched" (ISO time its store was last fetched in this
    process, None when never: a main store and a store with no remote are never fetched) and
    "fetch_error" (`fetch_say` of its last round's failure, or None).
    """

    def __init__(self, repos, refresh_s=None, clock=utc_now, opener=open_read, days=DAYS,
                 reuse_s=REUSE_S, wait=event_wait):
        self._repos = repos
        self.refresh_s = refresh_s
        self.clock = clock
        self.opener = opener
        self.days = days
        self.reuse_s = reuse_s
        self.wait = wait
        self.timer_error = None        # the class of what the timer's last round raised, or None
        self._cache = {}               # repo_view's: {name: (key, last good view)}
        self._held = {}                # {name: (root, read at, entry)}
        self._last = None              # the last answer whose repository list could be read
        self._fetched = {}
        self._fetch_error = {}
        self._fetching = threading.Lock()   # one round at a time
        self._rounds = 0
        self._life = threading.Lock()       # start() and stop()
        self._stop = None
        self._thread = None

    def repos(self):
        got = self._repos() if callable(self._repos) else self._repos
        return [(name, root) for name, root in got]

    def _read(self, name, root, now, opened=None):
        entry = repo_view(name, root, now, self.opener, False, self.days, self._cache, opened)
        self._held[name] = (root, now, entry)
        return entry

    def get(self, fresh=False):
        """The view now: {"at", "repos": [...], "error"}. The caller owns what it gets."""
        if fresh:
            return self.refresh()
        now = self.clock()
        try:
            pairs = self.repos()
        except Exception as exc:
            out = copy.deepcopy(self._last) if self._last else {"at": now.isoformat(), "repos": []}
            out["error"] = "%s: %s" % (NO_REPOS, exc.__class__.__name__)
            return out
        entries = []
        for name, root in pairs:
            held = self._held.get(name)
            if held and held[0] == root and 0 <= (now - held[1]).total_seconds() < self.reuse_s:
                entry = held[2]
            else:
                entry = self._read(name, root, now)
            entry = copy.deepcopy(entry)
            entry["fetched"] = self._fetched.get(name)
            entry["fetch_error"] = self._fetch_error.get(name)
            entries.append(entry)
        out = {"at": now.isoformat(), "repos": entries, "error": None}
        self._last = copy.deepcopy(out)
        return out

    def refresh(self):
        """Run a round (or take the one that began while this call waited), then `get()`."""
        seen = self._rounds
        with self._fetching:
            if self._rounds == seen:       # else: a round began after this call arrived
                self._round(None)
        return self.get()

    def _round(self, stop):
        """Fetch and read every repository, one after the other; the caller holds `_fetching`.
        `stop` (the timer's) ends the round between two repositories once it is set."""
        self._rounds += 1
        try:
            pairs = self.repos()
        except Exception:
            return                          # get() names it
        for name, root in pairs:
            if stop is not None and stop.is_set():
                return
            opened = None
            if root:
                try:
                    opened = self.opener(root, True)
                except Exception as exc:
                    self._fetch_error[name] = fetch_say(exc)
                else:
                    self._fetch_error.pop(name, None)
                    if opened[2]:
                        self._fetched[name] = self.clock().isoformat()
            self._read(name, root, self.clock(), opened)

    def start(self):
        """Start the timer: True when this call started its thread. False when there is no
        interval, or a thread is still alive (running, or not yet ended after a `stop(timeout)`
        that ran out): there is never a second one."""
        with self._life:
            if not self.refresh_s or self.refresh_s <= 0:
                return False
            if self._thread is not None:
                if self._thread.is_alive():
                    return False
                self._thread = None
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                            name="hub-backlog-refresh", daemon=True)
            self._thread.start()
            return True

    def _run(self, stop):
        while not stop.is_set():
            try:
                with self._fetching:
                    self._round(stop)
                self.timer_error = None
            except Exception as exc:
                self.timer_error = exc.__class__.__name__
            if self.wait(stop, self.refresh_s):
                return

    def stop(self, timeout=None):
        """End the timer and wait up to `timeout` seconds (None: until it ended) for its thread:
        True when no timer thread is left. The thread ends after the repository it is on, not the
        whole round; False means it was still on that one when the wait ran out."""
        with self._life:
            thread = self._thread
            if thread is None:
                return True
            self._stop.set()
            thread.join(timeout)
            if thread.is_alive():
                return False
            self._thread = None
            return True
