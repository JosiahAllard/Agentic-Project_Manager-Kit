"""kitlib.hubjobs.coord_restart: the hub server restarts a stopped coordinator on the account that
resets first among those under the limit, else its last account (docs/launcher.md "Hub restart").

  make(sources) -> jobs.Job | None   the job; None when launcher.state_dir is not set
  decide(key, entry, live, record, last_stop, down_runs, accounts, now, wake=None) -> Decision
                                     the whole restart decision, pure: no file, process or clock
                                     is read
  backlog_wake(offered, baseline)    a Wake when the offer holds a record id the baseline does not
  offered_ids(settings, key, entry)  the record ids `backlog.py next --all` offers the entry's
                                     coordinator (BacklogUnread when the store cannot be read)
  last_account(entry, record)        the account a coordinator last ran on, or None
  RestartJob(settings, ...)          its run(ctx): the boot one-shot, then read the state, decide
                                     each entry, start one
  boot_action(record, uptime_s, now) RUN, MARK or None: what the boot one-shot does, pure

Every EVERY_S the job lists the processes, reads sessions.json, stop.log and the usage files, and
runs decide() for each coordinator entry (an agent entry) that kitlib.pmstart.down_entries lists.
A Start runs `<kit>/tools/launch.py start --session KEY --account N` as a child, the line the PM
checklist prints, at most one per run. What it pushes to the PM (ctx.event, detail `KEY:aN`):
`restarted` (or `restarted-unchecked` when the account has no usage file, so its limit was not
checked, as launch.py's own headroom check does), `over-limit`, `not-signed-in` and `gave-up`,
each once per time the coordinator is down. The hub keeps one owed push per job and a newer one
replaces it, so a run pushes at most one event: one word, its detail every `KEY:aN` of that word
that fits the 64-character field, joined by '.'. Only what that event carries counts as told; the
rest goes out on a later run. A start that ends a failing streak (jobs.FAIL_PUSH failures in a row,
so the framework pushes `recovered` straight after the run, replacing anything owed) pushes its
`restarted` on the next run instead. A child that exits non-zero or does not end in
START_TIMEOUT_S is a failure (health), and counts as a start for the backoff and the cap.

Held, never started: a live session of the entry's stem (any account or recycle, the test
`launch.py start --session` refuses on: never a second copy); a deliberate stop, meaning a
stop.log line with no successor for a session of the stem (`launch.py stop --name X` without
--successor, the PM's step on a `ready to stop:` reply) newer than the last time the job saw the
coordinator alive, or with no sighting at all; a coordinator seen down on fewer than DOWN_RUNS runs
in a row (a hand-off gets time to start it first); START_CAP starts in
CAP_WINDOW_S already; a start sooner than BACKOFF_S after the last.

A wake lifts the deliberate-stop hold, and only it (every other hold still applies): the repo has
gained startable records, meaning `backlog.py next --all` for the coordinator's session (its
sessions.json key, and no session too when the key is its repo's name, the repo's main coordinator)
offers a record id that it did not offer on the first run that saw that stop (the baseline, kept as
`held` {stop_at, ids}). So a coordinator woken for some records that stops again is not woken for
them again. The offer is read only for an entry held on a deliberate stop, through kitlib.backlog
(opener.open_store with no fetch, plan.offer_for), from the store the repo's .kit.json names, else
the PM data worktree's (store.pm_data_live_store). A store that cannot be read wakes nothing and is
a note, not a failure. A wake start passes `--prompt` with the reason and the new ids.

The account: accounts.restart_pick, the signed-in account with a reading no older than
pmwatch.STALE_MINUTES under accounts.HEADROOM_LIMIT of both windows whose week resets first (a
missing, stale or unknown reading never counts as under). With none, the last account as before,
held when it is not known, not signed in, or at or over accounts.HEADROOM_LIMIT of a window that
has not reset; a stale reading under the limit still starts there.

The hub is the only thing that restarts agents (the launcher's sign-in task is retired,
kitlib.launcher.autostart). After a boot it also runs, once, `launch.py after-reboot` as a
detached windowless child: it starts the plain sessions and resumes the PM, and leaves the
coordinators to this job. It runs once per boot, at any uptime: on the hub process's first run
whose boot (now - uptime) is not the one kept, so a late sign-in still gets it and a hub restart in
the same boot does not. With no boot kept at all (a first install, a lost file) and the machine up
BOOT_S or more, the boot is only kept: that hub start is not a boot's. A bad state file is read as
no boot kept and nothing is written. after-reboot logs to after-reboot.log itself.

State kept across hub restarts, in <launcher.state_dir>/coord-restart.json:
{key: {seen_at, account, starts, held}}: the last time a session of the stem was seen running, the
account it ran on, the epoch seconds of the job's own starts, and the wake baseline of the
deliberate stop it is held on (dropped once it is seen running); and BOOT_KEY: {boot_at}, the
boot (epoch seconds) after-reboot last ran for. The down count and what was pushed are in memory
only, so a hub restart waits DOWN_RUNS runs again.
"""
import collections
import datetime
import math
import os
import re
import subprocess
import sys
import time

from kitlib import pmstart, pmwatch
from kitlib.hubsite import jobs
from kitlib.launcher import accounts as A
from kitlib.launcher import lookup, state
from kitlib.launcher.settings import LaunchError
from kitlib.proc import spawn

NAME = 'coord-restart'
EVERY_S = 300
FIRST_S = 120                  # after the hub starts
BOOT_S = 30 * 60               # no boot kept and up this long: keep the boot, do not run
BOOT_SAME_S = 120              # two boot times this close are one boot (uptime and clock jitter)
BOOT_DRIFT = 1e-3              # ... or within this share of the uptime: NTP slews the wall clock,
                               # not the tick count, so now - uptime drifts ~10-100 ppm
RUN, MARK = 'run', 'mark'      # boot_action: start after-reboot, or only keep the boot
BOOT_KEY = '_after_reboot'     # coord-restart.json's record of the boot after-reboot ran for
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
STAMP_S = 1                    # stop.log stamps are whole seconds (pmwatch.stop_log_reader's allowance)
DETAIL_MAX = 64                # kitlib.hubsite.push field(): a header field's length
_STAMP = '%Y-%m-%d %H:%M:%S'
_FIRST = ('restarted', 'restarted-unchecked', 'gave-up', 'over-limit', 'not-signed-in')

WAKE_IDS_SHOWN = 5             # record ids a wake's reason names before "+N more"

AccountState = collections.namedtuple('AccountState', 'signed_in usage')
Wake = collections.namedtuple('Wake', 'reason ids')     # why a deliberate stop is started again


class BacklogUnread(Exception):
    """The entry's backlog store could not be read: no wake, and a note."""


class Decision(object):
    """start True: start on `account`. `word`: the event to push (None: nothing), `reason`: the
    note. `unchecked`: a start whose account had no usage file. `wake`: the Wake a start lifting a
    deliberate stop came from, else None."""

    def __init__(self, start, reason, account=None, word=None, unchecked=False, wake=None):
        self.start, self.reason, self.account = start, reason, account
        self.word, self.unchecked, self.wake = word, unchecked, wake

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


def deliberate_stop(record, last_stop):
    """True when `last_stop` (the newest stop with no successor of the stem, or None) is newer than
    the last sighting in `record`, or there is no sighting: the coordinator was stopped on purpose."""
    seen = (record or {}).get('seen_at')
    return last_stop is not None and (seen is None or last_stop + STAMP_S > seen)


def shown_ids(ids, most=WAKE_IDS_SHOWN):
    """'A, B, C' for up to `most` ids, then ' +N more'."""
    ids = list(ids)
    text = ', '.join(ids[:most])
    return text + (' +%d more' % (len(ids) - most) if len(ids) > most else '')


def backlog_wake(offered, baseline):
    """A Wake when `offered` (the record ids next --all offers the coordinator now) holds one that
    `baseline` (those it offered when the stop was first seen) does not, else None."""
    new = sorted(set(offered) - set(baseline or ()))
    if not new:
        return None
    return Wake('its repo has new startable records: %s' % shown_ids(new), new)


# owner_wake: an owner answer or approval for the repo as a reason (hub Q-1595, open) is not
# built. It would be one more predicate returning a Wake or None, passed to wake_of.


def wake_of(*wakes):
    """The first Wake that is not None, or None."""
    return next((w for w in wakes if w is not None), None)


def wake_prompt(wake):
    """The first message of a wake start: why, and what to do first."""
    return ('Started by the hub: %s. Read your state note, then run backlog next.' % wake.reason)


def decide(key, entry, live, record, last_stop, down_runs, accounts, now, wake=None):
    """The restart decision for one sessions.json entry. `live`: the names of the launched
    sessions running now; `record`: the job's {seen_at, account, starts} for it (or None);
    `last_stop`: epoch seconds of the newest stop.log line with no successor for a session of the
    stem (or None); `down_runs`: the runs in a row, this one included, it was seen down;
    `accounts`: {account text: AccountState(signed_in, usage data or None)}; `now`: epoch seconds;
    `wake`: a Wake, which lifts the deliberate-stop hold and no other. Reads nothing itself."""
    if not isinstance(entry, dict) or not entry.get('stem'):
        return _hold('no stem')
    if not entry.get('agent'):
        return _hold('a plain session: not restarted')
    if entry.get('autostart') is False:
        return _hold('autostart off')
    stem = entry['stem']
    if any((n or '').startswith(stem) for n in live):   # lookup.running_stem's test
        return _hold('running')
    if not deliberate_stop(record, last_stop):
        wake = None                     # a crash restart: nothing to lift, no wake prompt
    elif wake is None:
        return _hold('stopped on purpose (stop.log, no successor): left down')
    woke = '' if wake is None else 'woken (%s): ' % wake.reason
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
    when = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    pick = A.restart_pick(accounts, when, acct, pmwatch.STALE_MINUTES, A.HEADROOM_LIMIT)
    if pick is not None:
        return Decision(True, woke + 'start on account %s: a fresh reading under %d%%, its week '
                        'resets first' % (pick, A.HEADROOM_LIMIT), pick, 'restarted', wake=wake)
    # no account with a fresh reading under the limit: the last account, as before
    if acct is None:
        return _hold('no last account known')
    st = accounts.get(acct)
    if st is None or not st.signed_in:
        return _hold('account %s is not signed in' % acct, 'not-signed-in', acct)
    if st.usage is not None:
        over = A.over_limit(st.usage, when, A.HEADROOM_LIMIT)
        if over:
            return _hold('account %s is at %s' % (acct, ' and '.join(over)), 'over-limit', acct)
    return Decision(True, woke + 'start on account %s' % acct, acct,
                    'restarted-unchecked' if st.usage is None else 'restarted', st.usage is None,
                    wake=wake)


def local_epoch(text, mktime=time.mktime, localtime=time.localtime):
    """Epoch seconds of a local 'YYYY-MM-DD HH:MM:SS' stamp. In the repeated hour when daylight
    saving ends the stamp names two instants: the later is taken, so the stop can read up to an
    hour late and a sighting after it can still count as older (docs/launcher.md "Hub restart")."""
    st = time.strptime(text, _STAMP)
    fits = []
    for dst in (0, 1):
        try:
            t = mktime(tuple(st[:8]) + (dst,))
        except (OverflowError, ValueError):
            continue
        lt = localtime(t)
        if tuple(lt[:6]) == tuple(st[:6]) and lt.tm_isdst == dst:
            fits.append(t)
    return max(fits) if fits else mktime(st)


def stop_times(text, epoch=None):
    """{name: epoch seconds of its newest stop with no successor} from stop.log's text. `epoch`
    turns a local 'YYYY-MM-DD HH:MM:SS' into epoch seconds (default local_epoch)."""
    epoch = epoch or local_epoch
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


def boot_action(record, uptime_s, now):
    """What the boot one-shot does on this run, once per boot. `record` is the kept {boot_at} (or
    None), `uptime_s` seconds since boot (inf when unknown). None when the uptime is unknown or the
    kept boot is within boot_slack(uptime_s) of this one (now - uptime_s); RUN when a different boot is kept,
    at any uptime, or none is and the machine has been up less than BOOT_S; MARK (keep this boot,
    do not run) when none is kept and it has been up longer. Reads nothing itself."""
    if not isinstance(uptime_s, (int, float)) or not math.isfinite(uptime_s):
        return None
    done = (record or {}).get('boot_at')
    if isinstance(done, (int, float)) and not isinstance(done, bool):
        return None if abs(done - (now - uptime_s)) <= boot_slack(uptime_s) else RUN
    return RUN if uptime_s < BOOT_S else MARK


def boot_slack(uptime_s):
    """How far two boot times may be apart and still be one boot: BOOT_SAME_S, or BOOT_DRIFT of
    the uptime when that is more (a long uptime gathers clock drift). A real reboot differs by the
    whole of the boot before it, far past either."""
    return max(BOOT_SAME_S, uptime_s * BOOT_DRIFT)


def uptime_s():
    """Seconds since boot (kitlib.launcher.host.Host.uptime_minutes), inf when unknown."""
    from kitlib.launcher import host
    return host.Host().uptime_minutes() * 60.0


def after_reboot_argv(kit):
    return [sys.executable, os.path.join(kit, 'tools', 'launch.py'), 'after-reboot']


def spawn_detached(argv, cwd):
    """Start one windowless child and do not wait: after-reboot waits 60 s and opens a window per
    session, longer than a run should take. Its outcome is in after-reboot.log."""
    return spawn.popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=dict(os.environ, PYTHONIOENCODING='utf-8'),
                       creationflags=_NOWIN)


def start_argv(kit, key, account, prompt=None):
    argv = [sys.executable, os.path.join(kit, 'tools', 'launch.py'), 'start', '--session', key,
            '--account', str(account)]
    return argv + (['--prompt', prompt] if prompt else [])


def wake_sessions(key, entry):
    """The backlog `--session` values whose offer is the coordinator's: its sessions.json key, and
    None (records with no `session:`) too when the key is its repo's name (the repo's main
    coordinator, as kitlib.launcher.handoff tells one)."""
    return [key] + ([None] if key == (entry or {}).get('repo') else [])


def store_root(settings, repo):
    """The checkout whose configured backlog store holds `repo`'s records: the repo's folder in
    launcher.repos when its .kit.json names a "backlog".store, else its PM data worktree when that
    names one (store.pm_data_live_store), else the repo's folder (the implicit store).
    BacklogUnread when the repo is not in launcher.repos, its .kit.json cannot be read, or its PM
    data worktree cannot be found or read: a store that may be the wrong one is never read."""
    from kitlib import config as kconfig
    from kitlib.backlog import store as bstore
    try:
        root = settings.repo_path(repo)
    except LaunchError:
        raise BacklogUnread('repo %r is not in launcher.repos' % (repo,))
    try:
        cfg = kconfig.load(root)
    except (kconfig.ConfigError, OSError) as exc:
        raise BacklogUnread('its .kit.json cannot be read (%s)' % type(exc).__name__)
    if cfg.written('backlog', 'store') is not None:
        return root
    try:
        pm_root, named, why = bstore.pm_data_live_store(root)
    except Exception as exc:                         # noqa: BLE001 - any failure is unread
        raise BacklogUnread('its PM data worktree cannot be found (%s)' % type(exc).__name__)
    if why:
        raise BacklogUnread('its PM data worktree cannot be found or read')
    return pm_root if pm_root is not None and named is not None else root


def offered_ids(settings, key, entry):
    """The record ids `backlog.py next --all` offers the coordinator of sessions.json entry
    `key` (every session of wake_sessions, the store of store_root), with no fetch: the hub's own
    backlog fetch keeps the local store fresh. The same offer `next` prints (plan.offer_for: live
    pulls, unmet `requires:`, the fold-in guard). BacklogUnread when it cannot be read."""
    from kitlib.backlog import opener, plan
    from kitlib.backlog import offer as omod
    from kitlib.backlog.errors import Refusal
    from kitlib.config import ConfigError
    root = store_root(settings, (entry or {}).get('repo'))
    ids, snap, holding = set(), None, None
    try:
        for session in wake_sessions(key, entry):
            ctx = opener.open_store(root, fetch=False, session=session)
            if snap is None:            # one read of the store and its live pulls for both offers
                snap = ctx.store.load()
                holding, _dead, _why = omod.in_flight_split(snap, ctx.root, ctx.remote)
            offers, _passed, _holding, _own = plan.offer_for(snap, ctx, cap=None, holding=holding)
            ids.update(o.rec.id for o in offers)
    except (Refusal, ConfigError, omod.PartitionError, OSError) as exc:
        raise BacklogUnread('the store could not be read (%s)' % type(exc).__name__)
    return ids


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
    run_child), `clock` epoch seconds, `kit` the kit checkout whose tools/launch.py runs,
    `uptime()` seconds since boot (default uptime_s), `detached(argv, cwd)` the after-reboot
    start (default spawn_detached), `backlog(key, entry)` the ids the coordinator is offered
    (default offered_ids; BacklogUnread when unread)."""

    def __init__(self, settings, procs=None, child=None, clock=None, kit=None, uptime=None, detached=None,
                 backlog=None):
        self.settings = settings
        self.backlog = backlog or (lambda key, entry: offered_ids(settings, key, entry))
        self.procs = procs or pmstart.list_processes
        self.child = child or run_child
        self.clock = clock or time.time
        self.kit = kit or pmstart.KIT_ROOT
        self.uptime = uptime or uptime_s
        self.detached = detached or spawn_detached
        self._boot_checked = False      # this hub process has asked once whether after-reboot is due
        self._down = {}                 # key: runs in a row seen down
        self._told = {}                 # key: the word pushed while it has been down
        self._fails = 0                 # failures in a row, the framework's count (Skips keep it)
        self._carry = []                # a start's push kept for the next run (_run)

    def __call__(self, ctx):
        try:
            result = self._run(ctx)
        except Exception:
            self._fails += 1
            raise
        if not isinstance(result, jobs.Skip):
            self._fails = 0
        return result

    def _run(self, ctx):
        now = self.clock()
        try:
            records = self._read_records()
        except LaunchError:
            self._after_reboot(None, now)   # a bad state file: no boot kept, nothing written
            raise
        booted = self._after_reboot(records, now)     # first: a bad sessions.json must not stop it
        sessions = state.read_sessions(self.settings)
        live = {lookup.rc_name(p.get('CommandLine')) for p in lookup.launched(self.procs())}
        live.discard(None)
        changed = self._note_sightings(sessions, live, records, now)
        down = {k: e for k, e in pmstart.down_entries(sessions, live)
                if not k.startswith('_') and e.get('agent') and e.get('stem')}
        for key in list(self._down):
            if key not in down:
                self._down.pop(key, None)
                self._told.pop(key, None)
        stops = accts = None
        notes, started, owed = ([booted] if booted else []), None, []
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
            last_stop = stem_stop(stops, entry['stem'])
            wake = None
            if deliberate_stop(records.get(key), last_stop):
                wake, note, moved = self._wake(key, entry, records, last_stop)
                changed = changed or moved
                if note:
                    notes.append(note)
            d = decide(key, entry, live, records.get(key), last_stop, self._down[key], accts, now,
                       wake=wake)
            if not d.start:
                notes.append('%s: %s' % (key, d.reason))
                if d.word and self._told.get(key) != d.word:
                    owed.append((key, d))
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
            self._start(key, d)
            notes.insert(0, '%s: started on account %s%s' % (
                key, d.account, '' if d.wake is None else ' (woken: %s)' % d.wake.reason))
            if self._fails >= jobs.FAIL_PUSH:
                # the framework pushes `recovered` after this run, which would replace this push
                self._carry.append((key, d))
                owed = []
            else:
                owed.insert(0, (key, d))
        elif self._carry:
            owed = self._carry + owed
            self._carry = []
        self._tell(ctx, owed)
        if changed:
            self._write_records(records)
        if started is None and not booted:
            return jobs.Skip('; '.join(notes) or 'every coordinator is running')
        return '; '.join(notes)

    def _after_reboot(self, records, now):
        """The boot one-shot, by boot_action: start `launch.py after-reboot` (RUN), then keep the
        boot in `records` and write them; MARK only keeps it. A child that cannot start is a job
        failure, tried again on the next run. `records` None is a state file that could not be
        read: no boot is kept and nothing is written. Asked until it is settled on this hub
        process's first run: a later run would only see the same boot. The note, or None."""
        if self._boot_checked:
            return None
        up = self.uptime()
        action = boot_action((records or {}).get(BOOT_KEY), up, now)
        if action == RUN:
            argv = after_reboot_argv(self.kit)
            try:
                self._boot_child = self.detached(argv, self.kit)
            except OSError as e:
                raise StartFailed('launch.py after-reboot could not start: %s' % e)
        self._boot_checked = True
        if action is None:
            return None
        if records is not None:
            records[BOOT_KEY] = {'boot_at': now - up}
            self._write_records(records)
        if action == MARK:
            return None
        return 'after a boot: launch.py after-reboot started (plain sessions and the PM; after-reboot.log)'

    def _wake(self, key, entry, records, last_stop):
        """(Wake or None, note or None, records changed) for an entry held on the deliberate stop
        `last_stop`. The first run that sees that stop keeps the offer as its baseline and wakes
        nothing; a later run wakes on an id outside it (backlog_wake). A store that cannot be read
        is a note, never a failure, and leaves the baseline as it was."""
        try:
            offered = self.backlog(key, entry)
        except BacklogUnread as e:
            return None, '%s: backlog not read, so not woken: %s' % (key, e), False
        rec = records.setdefault(key, {})
        held = rec.get('held')
        if not isinstance(held, dict) or held.get('stop_at') != last_stop:
            rec['held'] = {'stop_at': last_stop, 'ids': sorted(offered)}
            return None, None, True
        return wake_of(backlog_wake(offered, held.get('ids'))), None, False

    def _start(self, key, d):
        argv = start_argv(self.kit, key, d.account, wake_prompt(d.wake) if d.wake else None)
        try:
            code, lines = self.child(argv, self.kit)
        except subprocess.TimeoutExpired:
            raise StartFailed('launch.py start --session %s did not end in %d s' % (key, START_TIMEOUT_S))
        except OSError as e:
            raise StartFailed('launch.py start --session %s could not start: %s' % (key, e))
        if code != 0:
            raise StartFailed('launch.py start --session %s --account %s exited %s: %s'
                              % (key, d.account, code, (lines or ['no output'])[-1]))

    def _tell(self, ctx, owed):
        """Push one event for this run's `owed` [(key, Decision)]: the first word by _FIRST,
        detail every key of that word that fits. Only those are marked told: the hub keeps one
        owed push per job, so a second event would replace the first."""
        if not owed:
            return
        word = min((d.word for _, d in owed), key=lambda w: _FIRST.index(w) if w in _FIRST else len(_FIRST))
        detail = None
        for key, d in owed:
            if d.word != word:
                continue
            item = key if d.account is None else '%s:a%s' % (key, d.account)
            if detail is not None and len(detail) + 1 + len(item) > DETAIL_MAX:
                continue
            detail = item if detail is None else detail + '.' + item
            self._told[key] = word
        ctx.event(word, detail)

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
            rec.pop('held', None)       # running again: the stop it was held on is over
            acct = name_account(mine[-1])
            if acct is not None:
                rec['account'] = acct
            changed = True
        return changed

    def _accounts(self):
        out = {}
        for n in self.settings.account_numbers():
            _, data = A.newest_usage(self.settings, n, by='updated')   # the reading whose age is judged
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
