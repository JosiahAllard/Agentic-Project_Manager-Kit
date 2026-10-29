"""kitlib.hubjobs.coord_restart: the hub server restarts a stopped coordinator on its last account
(docs/launcher.md "Hub restart").

  make(sources) -> jobs.Job | None   the job; None when launcher.state_dir is not set
  decide(key, entry, live, record, last_stop, down_runs, accounts, now) -> Decision
                                     the whole restart decision, pure: no file, process or clock
                                     is read (the account-pick and sole-restarter work reuse it)
  last_account(entry, record)        the account a coordinator last ran on, or None
  RestartJob(settings, ...)          its run(ctx): read the state, decide each entry, start one

Every EVERY_S the job lists the processes, reads sessions.json, stop.log and the usage files, and
runs decide() for each coordinator entry (an agent entry) that kitlib.pmstart.down_entries lists.
A Start runs `<kit>/tools/launch.py start --session KEY --account N` as a child, the line the PM
checklist prints, at most one per run. What it pushes to the PM (ctx.event, detail `KEY:aN`):
`restarted` (or `restarted-unchecked` when the account has no usage file, so its limit was not
checked, as launch.py's own headroom check does), `over-limit`, `not-signed-in` and `gave-up`,
each once per time the coordinator is down. A child that exits non-zero or does not end in
START_TIMEOUT_S is a failure (health), and counts as a start for the backoff and the cap.

Held, never started: a live session of the entry's stem (any account or recycle, the test
`launch.py start --session` refuses on: never a second copy); a deliberate stop, meaning a
stop.log line with no successor for a session of the stem (`launch.py stop --name X` without
--successor, the PM's step on a `ready to stop:` reply) newer than the last time the job saw the
coordinator alive, or with no sighting at all; a coordinator seen down on fewer than DOWN_RUNS runs
in a row (the after-reboot task and a hand-off get time to start it first); START_CAP starts in
CAP_WINDOW_S already; a start sooner than BACKOFF_S after the last; no known account; an account
not signed in; an account at or over accounts.HEADROOM_LIMIT of a window that has not reset.

State kept across hub restarts, in <launcher.state_dir>/coord-restart.json:
{key: {seen_at, account, starts}}: the last time a session of the stem was seen running, the
account it ran on, and the epoch seconds of the job's own starts. The down count and what was
pushed are in memory only, so a hub restart waits DOWN_RUNS runs again.
"""
import collections
import datetime
import os
import re
import subprocess
import sys
import time

from kitlib import pmstart
from kitlib.hubsite import jobs
from kitlib.launcher import accounts as A
from kitlib.launcher import lookup, state
from kitlib.launcher.settings import LaunchError
from kitlib.proc import spawn

NAME = 'coord-restart'
EVERY_S = 300
FIRST_S = 120                  # after the hub starts: after-reboot has waited its 60 s by then
DOWN_RUNS = 2                  # runs in a row a coordinator must be seen down before a start
BACKOFF_S = (600, 1800, 5400)  # after the 1st, 2nd, 3rd start in the window
START_CAP = 3                  # starts per entry in CAP_WINDOW_S
CAP_WINDOW_S = 24 * 3600
START_TIMEOUT_S = 180          # launch.py start opens a window and polls ~20 s for its pipe
STATE_FILE = 'coord-restart.json'
STOP_LOG = 'stop.log'
_NOWIN = 0x08000000            # CREATE_NO_WINDOW
_STOP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) stopped '([^']*)' .*; successor '([^']*)'")
_ACCOUNT = re.compile(r'-a(\d+)(?:-r\d+)?\Z')

AccountState = collections.namedtuple('AccountState', 'signed_in usage')


class Decision(object):
    """start True: start on `account`. `word`: the event to push (None: nothing), `reason`: the
    note. `unchecked`: a start whose account had no usage file."""

    def __init__(self, start, reason, account=None, word=None, unchecked=False):
        self.start, self.reason, self.account = start, reason, account
        self.word, self.unchecked = word, unchecked

    def __repr__(self):
        return 'Decision(%r, %r, account=%r, word=%r)' % (self.start, self.reason, self.account, self.word)


def _hold(reason, word=None, account=None):
    return Decision(False, reason, account, word)


def of_stem(name, stem):
    """True when `name` is a session of `stem`: the stem itself, or <stem>-a<N>[-r<k>]."""
    return bool(name) and bool(stem) and (
        name == stem or re.match(re.escape(stem) + r'-a\d+(?:-r\d+)?\Z', name) is not None)


def name_account(name):
    """The N of a <stem>-a<N>[-r<k>] name, as text, or None."""
    m = _ACCOUNT.search(name or '')
    return m.group(1) if m else None


def last_account(entry, record):
    """The account the coordinator last ran on: the one the job last saw it running on, else the
    entry's sessions.json `account` (handoff and after-reboot write it; `launch.py start --session
    --account M` does not, hence the sighting first). None when neither is known."""
    seen = (record or {}).get('account')
    if seen not in (None, ''):
        return str(seen)
    acct = (entry or {}).get('account')
    return None if acct in (None, '') else str(acct)


def recent_starts(record, now):
    return sorted(t for t in ((record or {}).get('starts') or [])
                  if isinstance(t, (int, float)) and now - t < CAP_WINDOW_S)


def decide(key, entry, live, record, last_stop, down_runs, accounts, now):
    """The restart decision for one sessions.json entry. `live`: the names of the launched
    sessions running now; `record`: the job's {seen_at, account, starts} for it (or None);
    `last_stop`: epoch seconds of the newest stop.log line with no successor for a session of the
    stem (or None); `down_runs`: the runs in a row, this one included, it was seen down;
    `accounts`: {account text: AccountState(signed_in, usage data or None)}; `now`: epoch seconds.
    Reads nothing itself."""
    if not isinstance(entry, dict) or not entry.get('stem'):
        return _hold('no stem')
    if not entry.get('agent'):
        return _hold('a plain session: not restarted')
    if entry.get('autostart') is False:
        return _hold('autostart off')
    stem = entry['stem']
    if any((n or '').startswith(stem) for n in live):   # lookup.running_stem's test
        return _hold('running')
    seen = (record or {}).get('seen_at')
    if last_stop is not None and (seen is None or last_stop >= seen):
        return _hold('stopped on purpose (stop.log, no successor): left down')
    if down_runs < DOWN_RUNS:
        return _hold('down %d of %d runs needed' % (down_runs, DOWN_RUNS))
    starts = recent_starts(record, now)
    if len(starts) >= START_CAP:
        return _hold('%d starts in %d h already: left for the PM' % (len(starts), CAP_WINDOW_S // 3600),
                     'gave-up')
    if starts:
        wait = BACKOFF_S[min(len(starts), len(BACKOFF_S)) - 1]
        if now - starts[-1] < wait:
            return _hold('backoff: next start at most every %d s' % wait)
    acct = last_account(entry, record)
    if acct is None:
        return _hold('no last account known')
    st = accounts.get(acct)
    if st is None or not st.signed_in:
        return _hold('account %s is not signed in' % acct, 'not-signed-in', acct)
    if st.usage is not None:
        over = A.over_limit(st.usage, datetime.datetime.fromtimestamp(now, datetime.timezone.utc),
                            A.HEADROOM_LIMIT)
        if over:
            return _hold('account %s is at %s' % (acct, ' and '.join(over)), 'over-limit', acct)
    return Decision(True, 'start on account %s' % acct, acct,
                    'restarted-unchecked' if st.usage is None else 'restarted', st.usage is None)


def stop_times(text, epoch=None):
    """{name: epoch seconds of its newest stop with no successor} from stop.log's text. `epoch`
    turns a local 'YYYY-MM-DD HH:MM:SS' into epoch seconds (default time.mktime)."""
    epoch = epoch or (lambda s: time.mktime(time.strptime(s, '%Y-%m-%d %H:%M:%S')))
    out = {}
    for line in (text or '').splitlines():
        m = _STOP.match(line)
        if not m or m.group(3) not in ('-', ''):
            continue
        try:
            t = epoch(m.group(1))
        except (ValueError, OverflowError):
            continue
        out[m.group(2)] = max(t, out.get(m.group(2), t))
    return out


def stem_stop(stops, stem):
    """The newest of `stops` (stop_times) for a session of `stem`, or None."""
    hits = [t for n, t in stops.items() if of_stem(n, stem)]
    return max(hits) if hits else None


def start_argv(kit, key, account):
    return [sys.executable, os.path.join(kit, 'tools', 'launch.py'), 'start', '--session', key,
            '--account', str(account)]


def run_child(argv, cwd):
    """(exit code, output lines) of one windowless child, waited for up to START_TIMEOUT_S
    (subprocess.TimeoutExpired past it)."""
    r = spawn.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                  env=dict(os.environ, PYTHONIOENCODING='utf-8'), creationflags=_NOWIN,
                  timeout=START_TIMEOUT_S)
    return r.returncode, ((r.stdout or b'') + (r.stderr or b'')).decode('utf-8', 'replace').splitlines()


class StartFailed(Exception):
    """A `launch.py start` child that exited non-zero or did not end in time."""


def make(sources):
    from kitlib.launcher import settings as lsettings
    s = lsettings.load(getattr(sources, 'home', None))
    try:
        s.state_dir()
    except LaunchError:
        return None                     # no launcher state on this machine: nothing to restart
    return jobs.Job(NAME, EVERY_S, RestartJob(s), first_s=FIRST_S)


class RestartJob(object):
    """run(ctx) is the job. The parts a test replaces: `procs()` the process rows (default
    kitlib.pmstart.list_processes), `child(argv, cwd)` -> (exit code, lines) (default
    run_child), `clock` epoch seconds, `kit` the kit checkout whose tools/launch.py runs."""

    def __init__(self, settings, procs=None, child=None, clock=None, kit=None):
        self.settings = settings
        self.procs = procs or pmstart.list_processes
        self.child = child or run_child
        self.clock = clock or time.time
        self.kit = kit or pmstart.KIT_ROOT
        self._down = {}                 # key: runs in a row seen down
        self._told = {}                 # key: the word pushed while it has been down

    def __call__(self, ctx):
        now = self.clock()
        sessions = state.read_sessions(self.settings)
        live = {lookup.rc_name(p.get('CommandLine')) for p in lookup.launched(self.procs())}
        live.discard(None)
        records = self._read_records()
        changed = self._note_sightings(sessions, live, records, now)
        down = {k: e for k, e in pmstart.down_entries(sessions, live)
                if not k.startswith('_') and e.get('agent') and e.get('stem')}
        for key in list(self._down):
            if key not in down:
                self._down.pop(key, None)
                self._told.pop(key, None)
        stops = accts = None
        notes, started = [], None
        for key in sorted(down):
            entry = down[key]
            if any(n.startswith(entry['stem']) for n in live):    # another session of the stem runs
                self._down.pop(key, None)
                self._told.pop(key, None)
                continue
            self._down[key] = self._down.get(key, 0) + 1
            if started is not None:
                notes.append('%s: down; decided on the next run (one start per run)' % key)
                continue
            if stops is None:
                stops, accts = self._read_stops(), self._accounts()
            d = decide(key, entry, live, records.get(key), stem_stop(stops, entry['stem']),
                       self._down[key], accts, now)
            if not d.start:
                notes.append('%s: %s' % (key, d.reason))
                self._tell(ctx, key, d)
                continue
            if ctx.stop.is_set():
                notes.append('%s: start left for the next run (the hub is closing)' % key)
                continue
            started = (key, d)
        if started is not None:
            key, d = started
            rec = records.setdefault(key, {})
            rec['starts'] = recent_starts(rec, now) + [now]
            self._write_records(records)    # before the start: a crash mid-start still counts
            changed = False
            self._start(ctx, key, d)
            notes.insert(0, '%s: started on account %s' % (key, d.account))
        if changed:
            self._write_records(records)
        if started is None:
            return jobs.Skip('; '.join(notes) or 'every coordinator is running')
        return '; '.join(notes)

    def _start(self, ctx, key, d):
        argv = start_argv(self.kit, key, d.account)
        try:
            code, lines = self.child(argv, self.kit)
        except subprocess.TimeoutExpired:
            raise StartFailed('launch.py start --session %s did not end in %d s' % (key, START_TIMEOUT_S))
        except OSError as e:
            raise StartFailed('launch.py start --session %s could not start: %s' % (key, e))
        if code != 0:
            raise StartFailed('launch.py start --session %s --account %s exited %s: %s'
                              % (key, d.account, code, (lines or ['no output'])[-1]))
        self._told[key] = d.word
        ctx.event(d.word, '%s:a%s' % (key, d.account))

    def _tell(self, ctx, key, d):
        if d.word and self._told.get(key) != d.word:
            self._told[key] = d.word
            ctx.event(d.word, key if d.account is None else '%s:a%s' % (key, d.account))

    def _note_sightings(self, sessions, live, records, now):
        """Record seen_at and the account for every coordinator entry with a live session of its
        stem; True when anything changed."""
        changed = False
        for key, entry in state.entries(sessions):
            stem = entry.get('stem')
            if not stem or not entry.get('agent'):
                continue
            mine = sorted(n for n in live if of_stem(n, stem))
            if not mine:
                continue
            rec = records.setdefault(key, {})
            rec['seen_at'] = now
            acct = name_account(mine[-1])
            if acct is not None:
                rec['account'] = acct
            changed = True
        return changed

    def _accounts(self):
        out = {}
        for n in self.settings.account_numbers():
            _, data = A.newest_usage(self.settings, n)
            out[str(n)] = AccountState(A.account_signed_in(self.settings, n), data)
        return out

    def _path(self, name):
        return state.path(self.settings, name)

    def _read_records(self):
        """The state file's records; a missing file is none, a bad one a failure (LaunchError):
        reading it as empty would drop the cap."""
        data = state.read_json(self._path(STATE_FILE))
        return {k: v for k, v in (data or {}).items() if isinstance(v, dict)}

    def _write_records(self, records):
        state.write_json(self._path(STATE_FILE), records)

    def _read_stops(self):
        try:
            with open(self._path(STOP_LOG), encoding='utf-8', errors='replace') as f:
                return stop_times(f.read())
        except FileNotFoundError:
            return {}
