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

No text of git's, of an exception or of a path ever reaches a view: an "error" or a "fetch_error"
is one of the fixed phrases below, chosen by the class of the failure (`say`), with at most git's
exit code, a number.
"""
import copy
import datetime
import os
import re
import subprocess
import threading

from kitlib import config as kconfig
from kitlib import hubbacklog
from kitlib.backlog import store as bstore
from kitlib.backlog.errors import Refusal
from kitlib.git import cmd as gitcmd
from kitlib.git import refs

DAYS = hubbacklog.DAYS
REUSE_S = 5         # seconds `Views.get()` answers from what it read last, starting no git; a
                    # fresh read inside as many seconds of the last full round starts no fetch
PARKED_LOOKUPS = 20     # most `git log -1` runs one view build spends on parked records' times
NO_CHECKOUT = "no checkout of this repository on this machine"
NO_REPOS = "the repository list could not be read"
# The failure classes: every "error" and "fetch_error" is one of these, whole.
NO_FOLDER = "the repository's folder is missing"
NOT_A_REPO = "the folder is not a git repository"
GIT_NOT_STARTED = "git could not be started"
GIT_TIMED_OUT = "git timed out"
GIT_EXITED = "git exited %s"                # with the exit code
GIT_FAILED = "git failed"                   # non-zero, the code not known
BAD_REF = "the store's ref or the main branch could not be read"
BAD_STORE = "the backlog store could not be opened"
FETCH_FAILED = "the fetch failed"
FETCH_EXITED = FETCH_FAILED + " (git exited %s)"
READ_FAILED = "the read failed"
_MAIN = object()    # handed to a backend's landed(): a main store answers with it
_EXITED = re.compile(r"\bexited (-?\d{1,5})\b")
_TIMED = re.compile(r"\btimed out after \d+ seconds\b")
_GIT_SAID = re.compile(r"git [\w-]+: ")     # kitlib.hubbacklog.git's and the store's failures
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")
PHRASES = (NO_CHECKOUT, NO_FOLDER, NOT_A_REPO, GIT_NOT_STARTED, GIT_TIMED_OUT, GIT_FAILED, BAD_REF,
           BAD_STORE, FETCH_FAILED, READ_FAILED)
_CODED = re.compile("(?:%s|%s)$" % tuple(re.escape(p).replace("%s", r"-?\d{1,5}")
                                         for p in (GIT_EXITED, FETCH_EXITED)))


class ReadError(RuntimeError):
    """A read that failed, already put in its class: `said` is the fixed phrase (also its text)."""

    def __init__(self, said):
        RuntimeError.__init__(self, said)
        self.said = said


def fixed(text):
    """True when `text` is one of PHRASES, whole, or GIT_EXITED / FETCH_EXITED with a number."""
    return isinstance(text, str) and (text in PHRASES or bool(_CODED.match(text)))


def kind(text, default):
    """The fixed phrase for a failure whose own words are `text` (kitlib.git's `why`, an exception's
    text). The words only choose the class; none of them is returned, bar an exit code's digits."""
    text = text or ""
    if "could not run git" in text:
        return GIT_NOT_STARTED
    if _TIMED.search(text):
        return GIT_TIMED_OUT
    if "not a git repository" in text.lower():
        return NOT_A_REPO
    m = _EXITED.search(text)
    return GIT_EXITED % m.group(1) if m else default


def say(exc, root=None):
    """Why a read of the repository at `root` failed, as a view may show it: a fixed phrase. A
    ReadError's `said` is shown only when it is one (`fixed`); any other text reads READ_FAILED."""
    if isinstance(exc, ReadError):
        return exc.said if fixed(exc.said) else READ_FAILED
    if root and not os.path.isdir(root):
        return NO_FOLDER
    if isinstance(exc, subprocess.TimeoutExpired):
        return GIT_TIMED_OUT
    if isinstance(exc, OSError):
        return GIT_NOT_STARTED      # the one OSError a read meets: no git to start
    text = str(exc)
    return kind(text, GIT_FAILED if _GIT_SAID.match(text) else READ_FAILED)


def fetch_say(exc, root=None):
    """Why a fetch round's open of a repository failed, as a view may show it: FETCH_FAILED (with
    git's exit code when known) only when the fetch itself failed; a store that could not be
    opened at all is told as that (`say`)."""
    return say(exc, root)


def main_commit(root):
    """The commit of the main branch of the repository at `root`: the branch `.kit.json` names
    (repo.main_branch), else `main`, else `master`. ReadError when it is not there (a misnamed
    main branch is a failure, never an empty store) or git cannot tell."""
    named = (hubbacklog.kit_json(root).get("repo") or {}).get("main_branch")
    for branch in ([named] if named else ["main", "master"]):
        commit, why = refs.ref_commit("refs/heads/%s" % branch, root)
        if commit is None:
            raise ReadError(kind(why, BAD_REF))
        if commit:
            return commit
    raise ReadError(BAD_REF)


def open_read(root, fetch=False):
    """(commit, folder, fetched) the published records of the repository at `root` are read from,
    the store `.kit.json` names. `commit`: the full hash the store is at; None only when git
    answered that the store's ref does not exist (no store yet). `fetched`: True when a fetch ran.
    fetch=True first brings a remote ref store into its local ref; a main store, and a ref store
    in a repository with no remote, have nothing to fetch.

    ReadError (a RuntimeError holding its fixed phrase) when the folder is missing, the store
    cannot be opened or fetched, and whenever git gives no answer (it could not start, timed out,
    was ended, or refused the repository): only "no such ref" reads as empty (kitlib.git.refs
    ref_commit). Every git here runs through kitlib.git.cmd.call: scrubbed environment, a time
    limit, its own job."""
    if not os.path.isdir(root):
        raise ReadError(NO_FOLDER)
    fetched = False
    try:
        backend = bstore.open_backend(root, kconfig.load(root), fetch=fetch)
    except (kconfig.ConfigError, Refusal) as exc:
        raise ReadError(kind(str(exc), BAD_STORE))
    if fetch and isinstance(backend, bstore.RefBackend) and backend.remote:
        try:
            backend.load(None)
        except Refusal as exc:
            text = str(exc)
            if not text.startswith("cannot fetch"):
                raise ReadError(kind(text, BAD_STORE))
            m = _EXITED.search(text)
            raise ReadError(FETCH_EXITED % m.group(1) if m else FETCH_FAILED)
        fetched = True
    rev, folder = backend.landed(_MAIN)
    if rev is _MAIN:
        return main_commit(root), folder, fetched
    if rev.startswith("refs/"):
        commit, why = refs.ref_commit(rev, root)
    else:
        commit, why = refs.resolve(rev, root, known=True)
    if commit is None:
        raise ReadError(kind(why, BAD_REF))
    return (commit or None), folder, fetched


def window(now, days=DAYS):
    """The first day `completed` keeps, YYYY-MM-DD: the UTC date of `now` less `days` days. The
    window opens at that day's midnight UTC, whatever the time of day of the read."""
    return (now.astimezone(datetime.timezone.utc).date() - datetime.timedelta(days=days)).isoformat()


def parked_day(rec):
    """The record's `parked_at:` day, YYYY-MM-DD, or "" when it holds none or not a real date."""
    day = hubbacklog.first(rec, "parked_at")
    if not _DAY.match(day):
        return ""
    try:
        datetime.date(int(day[:4]), int(day[5:7]), int(day[8:]))
    except ValueError:
        return ""
    return day


def last_change(root, commit, path=None):
    """The committer date (ISO, with its offset) of the last commit at or before `commit` that
    changed `path`, or of `commit` itself with no path: one `git log -1` through kitlib.git.cmd,
    with its time limit. Never an exception: None when git gives no answer (it could not start,
    timed out, failed), "" when it answered and named no commit."""
    when, _why = gitcmd.run_text(["log", "-1", "--format=%cI", commit] + (["--", path] if path else []),
                                 root)
    return when if when is None else when.strip()


def parked_item(rec, changed):
    """What a parked record (`rec`, kitlib.hubbacklog.parse's) adds to its open item. Every key is
    always there, None when the record does not hold it:

    - "q": the question it is parked on (the record's `q:` line).
    - "parked_at" and "parked_at_from", the time and where it is from, one of three:
      "status": the record's own `parked_at:` line, given as it stands, a bare day YYYY-MM-DD. A
      bare day means "day only": it is the local calendar day `set-status parked` ran, with no
      time and no zone, so a page shows the age in whole days.
      "updated" (no `parked_at:` line, or not a real date): a full ISO timestamp, when the
      record's own file last changed.
      "store": a full ISO timestamp, when the store itself last changed. An upper bound only:
      the record was parked at or before it, so its age is at least what this gives.
      For the last two `changed()` answers (time, from) (`read_at` gives the one it uses); both
      None when it has no time.
    - "branch" (the record's `pulled_by:`) and "session" (its `session:`). Parking gives up the
      pull, so a record parked by tools/backlog.py holds no branch."""
    day = parked_day(rec)
    if day:
        when, how = day, "status"
    else:
        when, how = changed()
        when, how = (when, how) if when else (None, None)
    return {"q": hubbacklog.first(rec, "q") or None, "parked_at": when, "parked_at_from": how,
            "branch": hubbacklog.first(rec, "pulled_by") or None,
            "session": hubbacklog.first(rec, "session") or None}


def read_at(name, root, commit, folder, since, days=DAYS, flaws=None):
    """One repository's view from the store at `commit`: {"repo", "days", "open", "completed",
    "parked"}. `since` (YYYY-MM-DD, `window`) is the first closing day `completed` keeps: a
    `closed:` line of that day or later, or a file deleted at or after that day's midnight UTC
    (git is given the full timestamp; a bare date would mean "that day at this time of day").

    An open item whose state is `parked` also carries `parked_item`'s keys, and "parked" lists
    those same items: the open items with "s" == "parked".

    A parked record with no usable `parked_at:` day gets a time from git (`last_change`). The
    store commit's own date is asked first, once a build, when the first such record is met: the
    latest any record can have changed, the "store" time. Then the record's own file is asked
    ("updated"), for the first PARKED_LOOKUPS such records (in id order). The first lookup that
    gives no answer is the last lookup of this build: one hung git costs one time limit, and
    every record after it takes the "store" time (None when it was the store's lookup that gave
    none). No lookup fails the view; one that gave no answer adds a word to `flaws` (a list,
    the caller's), which says the view is not one to keep for the next read."""
    out = {"repo": name, "days": days, "open": [], "completed": [], "parked": []}
    if commit is None:
        return out
    spent, store_when, failed = [0], [], []

    def ask(path=None):
        when = last_change(root, commit, path)
        if when is None:
            failed.append(True)
            if flaws is not None:
                flaws.append("a parked record's time")
        return when

    def changed(path):
        if not store_when:
            store_when.append(ask())
        if not failed and spent[0] < PARKED_LOOKUPS:
            spent[0] += 1
            when = ask(path)
            if when:
                return when, "updated"
        return store_when[0], "store"

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
            item.update(parked_item(rec, lambda: changed("%s/%s.md" % (folder, rid))))
            out["parked"].append(dict(item))
    for rid, when, t in hubbacklog.deleted(root, commit, folder, since + "T00:00:00+00:00"):
        if rid not in records:  # deleted and later re-added: still in the store
            out["completed"].append({"id": rid, "t": t, "d": when})
    out["completed"].sort(key=lambda c: c["d"], reverse=True)
    return out


def repo_view(name, root, now, opener=open_read, fetch=False, days=DAYS, cache=None, opened=None):
    """One repository's view: `read_at`'s keys plus "error" (None, or why it could not be read:
    one of this module's fixed phrases, `say`) and "stale". A failed read keeps the lists of the last good read `cache` holds for this
    repository, with "stale": True; with none to keep the lists are empty.

    `cache` ({name: (key, view, whole)}, the caller's) spares the read while the store is at the
    same commit and the window on the same day, when the view it holds is whole: one built while
    a parked record's lookup gave no answer (`read_at`'s `flaws`) is kept only as the last good
    lists, and the next read builds it again. `opened`: what `opener` already answered for this
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
        if held and held[0] == key and held[2]:
            got = held[1]
        else:
            flaws = []
            got = read_at(name, root, commit, folder, since, days, flaws)
            if cache is not None:
                cache[name] = (key, got, not flaws)
    except Exception as exc:  # one repository's failure must not hide the others
        if held and held[0][0] == root:
            view.update(copy.deepcopy(held[1]))
            view["stale"] = True
        view["error"] = say(exc, root)
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
    started (0: every `get()` opens the store again), and seconds a full round answers a fresh
    read with no fetch started (0: every fresh read fetches). `wait`: `event_wait`, or a stand-in
    (tests).

    `get()` never fetches. A repository is read by one caller at a time (`_read`): a hung
    repository costs the caller that started its read git's time limit, and the callers that
    arrive meanwhile wait for that one read instead of starting their own. `refresh()` runs a
    round: each repository in turn is fetched and read again (the local copy when its fetch
    failed); `get(fresh=True)` is the same. Only one round runs at a time: a call that waited
    through a round started after it arrived takes that round's result instead of fetching again.
    `start()` runs a round at once and then every `refresh_s`, on a daemon thread, until `stop()`.

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
        self._held = {}                # {name: (root, the clock after its read, entry)}
        self._reads = {}               # {name: reads done}, counted under the name's lock
        self._locks = {}               # {name: its read lock}
        self._guard = threading.Lock()      # _locks
        self._round_at = None          # the clock when the last full round ended
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

    def _lock(self, name):
        """The lock that makes a repository's read single-flight: one per name, made once."""
        with self._guard:
            return self._locks.setdefault(name, threading.Lock())

    def _recent(self, at):
        """True when `at` (a clock reading, or None) is under `reuse_s` seconds old. A clock that
        went backwards (a negative age) is not recent."""
        return at is not None and 0 <= (self.clock() - at).total_seconds() < self.reuse_s

    def _read(self, name, root, now, opened=None):
        """Read one repository, never beside another read of it: a caller takes the entry of a
        read of this repository that ENDED after the caller arrived (it may have begun before),
        and starts no git of its own (a round's read, which brings what it `opened`, always
        reads). The entry is stamped with the clock after the read, so a slow read does not use
        up its own `reuse_s`; the clock is read before the read is counted, so a clock that
        raises leaves the waiting callers to read for themselves."""
        seen = self._reads.get(name, 0)
        with self._lock(name):
            held = self._held.get(name)
            if opened is None and held and held[0] == root and self._reads.get(name, 0) != seen:
                return held[2]
            entry = repo_view(name, root, now, self.opener, False, self.days, self._cache, opened)
            self._held[name] = (root, self.clock(), entry)
            self._reads[name] = self._reads.get(name, 0) + 1
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
            if held and held[0] == root and self._recent(held[1]):
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
        """Run a round, then `get()`. No round is run when one began while this call waited, or
        when the last full round ended under `reuse_s` seconds ago: that round's result serves."""
        seen = self._rounds
        with self._fetching:
            if self._rounds == seen and not self._recent(self._round_at):
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
                    self._fetch_error[name] = fetch_say(exc, root)
                else:
                    self._fetch_error.pop(name, None)
                    if len(opened) > 2 and opened[2]:   # a two-member answer: not fetched
                        self._fetched[name] = self.clock().isoformat()
            self._read(name, root, self.clock(), opened)
        self._round_at = self.clock()       # a round cut short is not one a fresh read may reuse

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
