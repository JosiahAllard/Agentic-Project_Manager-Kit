"""kitlib.hubjobs.usage_crossover: the hub moves a session to another account as its account nears
the weekly limit (docs/launcher.md "Usage crossover", docs/action-hub.md "Timed jobs").

  make(sources) -> jobs.Job | None   the job; None when launcher.state_dir is not set or the host
                                     has no watcher readers
  decide(key, entry, crossing, live, stall, quiet_s, accounts, newest, starts, running, landed,
         mode, now, note_at, cleared, asks) -> Decision
                                     one session's next step, pure: no file, process or clock read
  pick_target(accounts, current, running, landed, now) -> (account, why) | None
  claims(settings, now) -> {key}     the sessions the job is moving now (kitlib.hubjobs.coord_restart
                                     holds them); LaunchError for a bad state file
  CrossoverJob(load, ...)            its run(ctx)

The trigger is the weekly window only (the five-hour window is ignored): a reading no older than
pmwatch.STALE_MINUTES at TRIGGER_WEEK% or more, or a stall read: the session transcript's newest
entry is a weekly-limit error (the host's weekly_stop, not pm_watch's LIMIT event, which can lag
an hour) whose reset lies ahead, with no transcript write for QUIET_S.

launcher.usage_crossover, read again at each run, sets what the job may do. `notify` (the default,
and unset): decide, log, run `launch.py handoff --what-if` and `stop --dry-run` on each change and
push `would-cross`; nothing is stopped or started. `normal`: crossings that ask first. `full`:
those and the stop-first path for a stalled session.

A crossing asks first: the job pushes the session a checkpoint message and waits for its ready
record (hub_handoff_ready; kitlib.hubsite.api "Hand-offs"), newer by C-number than the newest it
noted when it asked. The states of a crossing, each written to disk before its action, so a hub
restart resolves each from live facts:

  asked       not delivered: deliver again. A newer `handoff` or `restart` record: ready. A newer
              `stop` record: cleared, the PM told. WAIT_READY_S with nothing: `no-ready` once, and
              it stays open. The week reset or the session gone: cleared. The session stalled and
              the mode not full: cleared with `stalled`, left to the PM (it cannot reply).
  ready       waits until the record's state note was written after the ask and the transcript
              has been quiet QUIET_S (WAIT_READY_S of it: `no-ready` once); then notes the highest
              C-number, counts the start, then `launch.py handoff --session K --account N --state
              <path>` (no --state, and no wait, on the stall path: the entry's note). The entry
              moved to the job's own successor name with no successor running: failed.
  starting    successor live and sessions.json moved: started. Successor live and the entry not
              moved: the child died between the two, so the job finishes the file write and never
              runs handoff again. No successor after START_WAIT_S: backoff, then ready.
  started     a newer `checkin` record (the successor's prompt tells it to post one): stopping.
              WAIT_CHECKIN_S with nothing: `no-checkin` once; the old session stays idle, and
              nothing is ever stopped on a timeout. The old session gone: done.
  stopping    `launch.py stop --name old --successor new --pid P`; the old session gone: done. A
              stop is counted before its child: after a refused one, BACKOFF_S; STOP_TRIES of
              them: failed, the old session stays (the stall stop the same).
  stall-stop  on `full`, from any state before stopping, when the old session is stalled:
              `launch.py stop --name old --pid P`; then ready, with `stalled` set, starting on the
              entry's own note. A successor already up is named to stop, and the crossing goes
              back to started. An old session no longer stalled is not stopped (a hub restart can
              reach this state late): back to started, or cleared with no successor.

Before a start, an entry whose name differs from the one noted was moved by someone else: cleared.
A crossing clears on done, abort or failure, with a push; the file then holds no claim. One that
clears with the session left where it was (ready to stop, its successor gone, its start's entry
write with no successor, its stop refused, an unknown state) is remembered for its key as
{account, reset}: the trigger asks it nothing more on that account until that week resets. A
stall on normal is still pushed (`stalled`); on full such a key is neither stopped nor pushed.

Holds, nothing done: the PM session; no entry, stem or state note; several processes of the stem;
the old process's pid or creation time changed; a stale reading with no stall read; a reset within
RESET_NEAR_S; no room on any account (no stop and no start: `no-room` once per change, and a stalled
session stays and wakes at its reset); KEY_CAP starts for the key or MACHINE_CAP on the machine in
CAP_WINDOW_S, counted before the child runs; ASK_CAP new crossings asked for the key in
CAP_WINDOW_S (`cross-failed`); BACKOFF_S after a failed start; a stall read with
activity under QUIET_S old. No second start goes to an account until its reading is newer than the
job's last start there.

The target (pick_target; never `auto`, never accounts.pick): tier 1, a signed-in account under
TARGET_LIMIT% of both windows whose week resets first; tier 2, the lowest week under TIER2_WEEK%
with five hours under TARGET_LIMIT%. Not the session's account, nor the account sessions.json
still names for it (handoff refuses that one). A stale reading counts only with no live session on
that account: an idle account's reading does not refresh. An unknown reading never counts.

One acting step per run across all keys, stop-stalled first, then stop-old, finish, start, ask,
would-cross; the rest wait for the next run. Every key is decided on each run.

The PM session is skipped: it gets a `pm-limit` push, once per week window, and no crossing here.
A live session of no sessions.json entry gets an `unmanaged` push the same way and is left alone.

Pushes (ctx.event, push.JOB_EACH: owed once per detail, each with its own text): crossing,
would-cross, crossed, cross-cleared, cross-failed, stalled, no-ready, no-checkin, no-room, pm-limit,
unmanaged. A word goes out once per change, kept in the state file as `told`.

State in <launcher.state_dir>/usage-crossover.json: {crossings: {key: {phase, phase_at, ...}},
starts: {key: [epoch]}, asks: {key: [epoch]}, landed: {account: epoch of the last start there},
told: {key: mark}, windows: {name: reset of the window already told}, readings: {account: text},
cleared: {key: {account, reset}}}. The same file is the
claim: coord-restart holds a key whose crossing moved phase less than CLAIM_TTL_S ago. A bad file
is a failure, here and in coord-restart, never read as empty.

usage-crossover.log, same folder: one line per reading change (the history a burn-rate trigger
would need; none is built) and one per changed decision.
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
from kitlib.hubjobs import coord_restart as C
from kitlib.hubjobs import session_watch
from kitlib.hubsite import jobs
from kitlib.launcher import accounts as A
from kitlib.launcher import handoff, lookup, pipes, state
from kitlib.launcher import stop as lstop
from kitlib.launcher.settings import LaunchError, NOTIFY, FULL

NAME = 'usage-crossover'
EVERY_S = 300
FIRST_S = 60                   # before coord-restart's 120: a claim is written before it reads one
TRIGGER_WEEK = 99              # % of the week; the five-hour window is ignored
TARGET_LIMIT = A.HEADROOM_LIMIT
TIER2_WEEK = 98
RESET_NEAR_S = 60 * 60         # a reset this close is waited for
QUIET_S = 60                   # no transcript write this long: a stall read is a stall
WAIT_READY_S = 30 * 60         # after asking: `no-ready`
WAIT_CHECKIN_S = 30 * 60       # after the start: `no-checkin`
START_WAIT_S = 5 * 60          # no successor this long after a start: failed
CLAIM_TTL_S = 2 * 3600         # after the last phase change
KEY_CAP = 3                    # starts per key in CAP_WINDOW_S
ASK_CAP = 3                    # new crossings asked per key in CAP_WINDOW_S
MACHINE_CAP = 6                # starts on the machine in CAP_WINDOW_S
BACKOFF_S = C.BACKOFF_S
STOP_TRIES = len(BACKOFF_S)    # stops of one old session run, each after the last one's backoff
CAP_WINDOW_S = C.CAP_WINDOW_S
STATE_FILE = 'usage-crossover.json'
LOG = 'usage-crossover.log'
WEEK_S = 7 * 24 * 3600

ASKED, READY, STARTING, STARTED, STOPPING, STALL_STOP = (
    'asked', 'ready', 'starting', 'started', 'stopping', 'stall-stop')
# What a Decision does. The first six act, one per run, in this order
STOP_STALLED, STOP_OLD, FINISH, START, ASK, WOULD = (
    'stop-stalled', 'stop-old', 'finish', 'start', 'ask', 'would')
ADVANCE, CLEAR, HOLD = 'advance', 'clear', 'hold'
ACTS = (STOP_STALLED, STOP_OLD, FINISH, START, ASK, WOULD)
READY_KINDS = ('handoff', 'restart')

Live = collections.namedtuple('Live', 'name pid created')
_C_NO = re.compile(r'^C-(\d+)\Z')


class StepFailed(Exception):
    """A launch.py child that exited non-zero or did not end in time."""


class Decision(object):
    """`action` one of ACTS, ADVANCE, CLEAR, HOLD. `note` the text for health and the log;
    `target` the account a start or a would-cross goes to; `word`, `detail` the push and `mark`
    what says it changed (default the word); `phase`, `fields` an ADVANCE's next state, or a
    CLEAR's {account, reset} to remember (`remember` asks _step to fill them)."""

    def __init__(self, action, note, target=None, word=None, detail=None, mark=None, phase=None,
                 fields=None, remember=False):
        self.action, self.note, self.target = action, note, target
        self.word, self.detail, self.mark = word, detail, mark or word
        self.phase, self.fields = phase, dict(fields or {})
        self.remember = remember

    def __repr__(self):
        return 'Decision(%r, %r, target=%r, word=%r, phase=%r)' % (
            self.action, self.note, self.target, self.word, self.phase)


def _hold(note, word=None, detail=None, mark=None):
    return Decision(HOLD, note, word=word, detail=detail, mark=mark)


def _epoch(text):
    t = A.parse_time(text)
    return None if t is None else t.timestamp()


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def window(data, name, now):
    """(pct, reset epoch) of a reading's window; (None, None) when it is unknown. A window whose
    reset has passed is 0%, and a week's reset moves on seven days at a time."""
    w = data.get(name) if isinstance(data, dict) else None
    used = w.get('used_percentage') if isinstance(w, dict) else None
    reset = _epoch(w.get('resets_at')) if isinstance(w, dict) else None
    if not _number(used) or reset is None:
        return None, None
    if reset > now:
        return float(used), reset
    if name == 'seven_day':
        reset += (math.floor((now - reset) / WEEK_S) + 1) * WEEK_S
    return 0.0, reset


def is_fresh(data, now):
    """True for a reading no older than pmwatch.STALE_MINUTES whole minutes."""
    updated = _epoch(data.get('updated_at')) if isinstance(data, dict) else None
    return updated is not None and (now - updated) // 60 <= pmwatch.STALE_MINUTES


def is_stalled(stall, quiet_s, now):
    """True when the stall read `stall` (account or None, reset epoch) lies ahead and the
    transcript has been quiet QUIET_S. An unknown quiet time is not quiet."""
    return stall is not None and stall[1] > now and quiet_s is not None and quiet_s >= QUIET_S


def c_number(doc):
    """The number of a hand-off record's id, or -1."""
    m = _C_NO.match(str((doc or {}).get('id')))
    return int(m.group(1)) if m else -1


def newer(doc, high):
    """`doc` when its C-number is above `high` (None: none noted), else None."""
    return doc if doc is not None and c_number(doc) > (-1 if high is None else high) else None


def recent(times, now):
    return sorted(t for t in (times or []) if _number(t) and now - t < CAP_WINDOW_S)


def cap_reached(key, starts, now):
    """The reason this key may not start now, or None."""
    mine = recent((starts or {}).get(key), now)
    if len(mine) >= KEY_CAP:
        return '%d starts in %d h for it already' % (len(mine), CAP_WINDOW_S // 3600)
    every = sum(len(recent(v, now)) for v in (starts or {}).values())
    if every >= MACHINE_CAP:
        return '%d starts in %d h on the machine already' % (every, CAP_WINDOW_S // 3600)
    return None


def pick_target(accounts, current, running, landed, now):
    """(account, why) of the account to move a session to, or None. `accounts`: {account:
    AccountState}; `current`: the accounts to leave out; `running`: {account: live sessions};
    `landed`: {account: epoch of the job's last start there}. Module docstring "The target"."""
    rows = []
    for acct in sorted(accounts, key=int):
        st = accounts[acct]
        if acct in current or not st.signed_in:
            continue
        five, _ = window(st.usage, 'five_hour', now)
        week, reset = window(st.usage, 'seven_day', now)
        if five is None or week is None:
            continue
        if not is_fresh(st.usage, now) and running.get(acct):
            continue
        last = (landed or {}).get(acct)
        updated = _epoch(st.usage.get('updated_at'))
        if last is not None and (updated is None or updated <= last):
            continue
        rows.append((acct, five, week, reset))
    tier1 = [r for r in rows if r[1] < TARGET_LIMIT and r[2] < TARGET_LIMIT]
    if tier1:
        pick = min(tier1, key=lambda r: r[3])
        return pick[0], 'under %d%%, its week resets first' % TARGET_LIMIT
    tier2 = [r for r in rows if r[1] < TARGET_LIMIT and r[2] < TIER2_WEEK]
    if tier2:
        pick = min(tier2, key=lambda r: r[2])
        return pick[0], 'the lowest week under %d%%' % TIER2_WEEK
    return None


def _skip(account, entry):
    """The accounts no target may be: the session's, and the one sessions.json names for it."""
    out = {str(account)}
    if isinstance(entry, dict) and entry.get('account') not in (None, ''):
        out.add(str(entry['account']))
    return out


def _no_room(key):
    return _hold('no account has room', 'no-room', key)


def decide(key, entry, crossing, live, stall, quiet_s, accounts, newest, starts, running, landed,
           mode, now, note_at=None, cleared=None, asks=None):
    """One session's next step. `entry` its sessions.json entry (None: gone); `crossing` its open
    crossing or None; `live` [Live] of the processes of its stem; `stall` the stall read of the
    session in question, or None; `quiet_s` seconds since its transcript was written, or None;
    `accounts` {account: AccountState}; `newest` its newest hand-off record or None; `starts`
    {key: [epoch]}; `running`, `landed` as pick_target; `mode` launcher.usage_crossover;
    `note_at` the mtime of the crossing's state note (its ready record's), or None; `cleared` the
    {account, reset} of its last crossing that cleared with the session left where it was, or
    None; `asks` [epoch] of its asks. Reads nothing itself."""
    if crossing:
        return _step(key, entry, crossing, live, stall, quiet_s, accounts, newest, starts, running,
                     landed, mode, now, note_at)
    return _trigger(key, entry, live, stall, quiet_s, accounts, starts, running, landed, mode, now,
                    cleared, asks)


def _cleared_now(cleared, acct, reset):
    """True when a crossing of this key cleared on this account in this week window."""
    return (isinstance(cleared, dict) and str(cleared.get('account')) == str(acct)
            and _number(cleared.get('reset')) and abs(cleared['reset'] - reset) < RESET_NEAR_S)


def _trigger(key, entry, live, stall, quiet_s, accounts, starts, running, landed, mode, now,
             cleared=None, asks=None):
    if not isinstance(entry, dict) or not entry.get('stem'):
        return _hold('no stem')
    if not entry.get('state'):
        return _hold('no state note in sessions.json')
    if not live:
        return _hold('not running')
    if len(live) > 1:
        return _hold('several processes of the stem: %s' % ', '.join(sorted(l.name for l in live)))
    stalled = is_stalled(stall, quiet_s, now)
    acct = (stall[0] if stall and stall[0] else None) or C.name_account(live[0].name) or (
        None if entry.get('account') in (None, '') else str(entry['account']))
    st = accounts.get(acct)
    data = st.usage if st is not None else None
    week, week_reset = window(data, 'seven_day', now)
    high = week is not None and week >= TRIGGER_WEEK
    fresh = high and is_fresh(data, now)
    if not stalled and not fresh:
        if high:
            return _hold('account %s: %d%% of its week, a stale reading, no stall read' % (acct, week))
        if stall is not None and stall[1] > now:
            return _hold('a stall read, but active under %d s ago' % QUIET_S)
        return _hold('under the trigger')
    reset = stall[1] if stalled else week_reset
    if reset - now < RESET_NEAR_S:
        return _hold('the week resets within %d min' % (RESET_NEAR_S // 60))
    if stalled and mode not in (FULL, NOTIFY):     # before the cleared hold, which says nothing
        return _hold('stalled, left to the PM (usage_crossover is not full)', 'stalled', key)
    if _cleared_now(cleared, acct, reset):
        return _hold('its crossing cleared on account %s this week: not asked again before the '
                     'week resets' % acct)
    why = cap_reached(key, starts, now)
    if why:
        return _hold(why, 'cross-failed', key, 'cap')
    pick = pick_target(accounts, _skip(acct, entry), running, landed, now)
    if pick is None:
        return _no_room(key)
    target, reason = pick
    how = 'stalled' if stalled else '%d%% of its week' % week
    note = 'account %s %s: to account %s (%s)' % (acct, how, target, reason)
    detail = '%s:a%s' % (key, target)
    if mode == NOTIFY:
        return Decision(WOULD, note, target, 'would-cross', detail,
                        'would-cross:%s:%s' % (target, 'stall' if stalled else 'week'))
    if stalled:                 # full (normal held above): it cannot reply to a checkpoint message
        return Decision(STOP_STALLED, note, target, 'stalled', detail)
    n = len(recent(asks, now))
    if n >= ASK_CAP:
        return _hold('%d asks in %d h for it already' % (n, CAP_WINDOW_S // 3600), 'cross-failed',
                     key, 'ask-cap')
    return Decision(ASK, note, target, 'crossing', detail)


def _gone(note, word='cross-cleared', detail=None, remember=False):
    """A CLEAR. `remember`: the session stays where it is, so the trigger must not ask it again
    in this week window (_cleared_now)."""
    return Decision(CLEAR, note, word=word, detail=detail, remember=remember)


def _advance(phase, note, **fields):
    return Decision(ADVANCE, note, phase=phase, fields=fields)


def _step(key, entry, c, live, stall, quiet_s, accounts, newest, starts, running, landed, mode, now,
          note_at=None):
    d = _phase_step(key, entry, c, live, stall, quiet_s, accounts, newest, starts, running, landed,
                    mode, now, note_at)
    if d.action == CLEAR and d.remember:
        acct = str(c.get('account'))
        st = accounts.get(acct)
        reset = stall[1] if stall is not None and stall[1] > now else window(
            st.usage if st is not None else None, 'seven_day', now)[1]
        d.fields = {} if reset is None else {'account': acct, 'reset': reset}
    return d


def _phase_step(key, entry, c, live, stall, quiet_s, accounts, newest, starts, running, landed,
                mode, now, note_at):
    phase = c.get('phase')
    if not isinstance(entry, dict):
        return _gone('its sessions.json entry is gone', detail=key)
    if mode == NOTIFY:
        if phase in (ASKED, READY):
            return _gone('usage_crossover is notify: crossing dropped', detail=key)
        return _hold('usage_crossover is notify: %s left as it is' % phase)
    old = [l for l in live if l.pid == c.get('old_pid') and l.created == c.get('old_created')]
    new = c.get('new_name')
    up = [l for l in live if new and l.name == new]
    moved = bool(new) and entry.get('name') == new
    args = (key, entry, c, live, old, up, moved)
    stall_now = bool(old) and is_stalled(stall, quiet_s, now)
    stalled = stall_now and mode == FULL
    if phase == ASKED:
        return _asked(args, stalled, stall_now, accounts, newest, running, landed, now)
    if phase == READY:
        return _ready(args, stalled, accounts, starts, running, landed, now, quiet_s, note_at)
    if phase == STARTING:
        return _starting(args, c, now)
    if phase == STARTED:
        return _started(args, stalled, newest, now)
    if phase == STOPPING:
        return _stopping(args, now)
    if phase == STALL_STOP:
        return _stall_stop(args, stalled, now)
    return _gone('unknown state %r' % (phase,), 'cross-failed', key, remember=True)


def _stall_first(key, entry, c, accounts, running, landed, now):
    """The step from asked or ready when the old session stalls: on to stall-stop with a target."""
    pick = pick_target(accounts, _skip(c.get('account'), entry), running, landed, now)
    if pick is None:
        return _no_room(key)
    return _advance(STALL_STOP, 'stalled: stop first, then account %s' % pick[0], stalled=True)


def _asked(args, stalled, stall_now, accounts, newest, running, landed, now):
    key, entry, c, live, old, _up, _moved = args
    if not old:
        return _gone('the session ended before its ready record', detail=key)
    if entry.get('name') != c.get('entry_name'):
        return _gone('sessions.json was moved by someone else', detail=key)
    if len(live) > 1:
        return _hold('several processes of the stem')
    rec = newer(newest, c.get('high_c'))
    if rec is not None and rec.get('kind') == 'stop':
        return _gone('it replied ready to stop: no crossing', detail=key, remember=True)
    if rec is not None and rec.get('kind') in READY_KINDS:
        return _advance(READY, 'ready record %s' % rec.get('id'), state_path=rec.get('state_path'))
    if stalled:
        return _stall_first(key, entry, c, accounts, running, landed, now)
    if stall_now:               # stopped at the limit before its reply: it cannot send one now
        return _gone('stalled before its ready record: left to the PM (usage_crossover is not '
                     'full)', 'stalled', key)
    st = accounts.get(str(c.get('account')))
    week, _ = window(st.usage if st is not None else None, 'seven_day', now)
    if week is not None and week < TRIGGER_WEEK:
        return _gone('the week reset: no crossing needed', detail=key)
    if not c.get('delivered'):
        return Decision(ASK, 'the checkpoint message again: not delivered', c.get('target'))
    if now - c.get('phase_at', now) >= WAIT_READY_S:
        return _hold('no ready record in %d min' % (WAIT_READY_S // 60), 'no-ready', key)
    return _hold('waiting for its ready record')


def _settled(key, c, quiet_s, note_at, now):
    """None when the old session has settled on its hand-off: its state note written after the
    ask and its transcript quiet QUIET_S. Else the hold; `no-ready` after WAIT_READY_S of it."""
    asked = c.get('asked_at')
    if not (_number(note_at) and _number(asked) and note_at > asked):
        why = 'its state note %s not written since the ask' % c.get('state_path')
    elif quiet_s is None or quiet_s < QUIET_S:
        why = 'its transcript written under %d s ago' % QUIET_S
    else:
        return None
    if now - c.get('phase_at', now) >= WAIT_READY_S:
        return _hold('%s, for %d min' % (why, WAIT_READY_S // 60), 'no-ready', key)
    return _hold('waiting: ' + why)


def _ready(args, stalled, accounts, starts, running, landed, now, quiet_s=None, note_at=None):
    key, entry, c, live, old, up, _moved = args
    if up:
        return _advance(STARTING, 'its successor is up')
    was_stalled = bool(c.get('stalled'))
    if was_stalled and live:
        return _gone('someone started it again', detail=key)
    if entry.get('name') != c.get('entry_name'):
        if c.get('new_name') and entry.get('name') == c['new_name']:
            return _gone('its start moved sessions.json to %s, which is not running%s'
                         % (c['new_name'], '; the old session stays' if old else ''),
                         'cross-failed', key, remember=True)
        return _gone('sessions.json was moved by someone else', detail=key)
    if not was_stalled:
        if not old:
            return _gone('the session ended before its successor started', detail=key)
        if len(live) > 1:
            return _hold('several processes of the stem')
        if stalled:
            return _stall_first(key, entry, c, accounts, running, landed, now)
        wait = _settled(key, c, quiet_s, note_at, now)
        if wait is not None:
            return wait
    why = cap_reached(key, starts, now)
    if why:
        return _gone(why, 'cross-failed', key)
    retry = c.get('retry_at')
    if _number(retry) and now < retry:
        return _hold('backoff after a failed start')
    pick = pick_target(accounts, _skip(c.get('account'), entry), running, landed, now)
    if pick is None:
        return _no_room(key)
    return Decision(START, 'to account %s (%s)' % pick, pick[0])


def _starting(args, c, now):
    _key, _entry, _c, _live, _old, up, moved = args
    if up and moved:
        return _advance(STARTED, 'its successor is up')
    if up:
        return Decision(FINISH, 'its successor is up, sessions.json not moved')
    if now - c.get('phase_at', now) < START_WAIT_S:
        return _hold('waiting for its successor')
    fails = int(c.get('fails') or 0) + 1
    wait = BACKOFF_S[min(fails, len(BACKOFF_S)) - 1]
    return _advance(READY, 'no successor in %d s: backoff %d s' % (START_WAIT_S, wait), fails=fails,
                    retry_at=now + wait)


def _started(args, stalled, newest, now):
    key, _entry, c, _live, old, up, _moved = args
    if not old:
        if up:
            return _gone('its old session has gone: done', 'crossed', c.get('new_name'))
        return _gone('both sessions have gone', 'cross-failed', key)
    if not up:
        return _gone('its successor has gone; the old session stays', 'cross-failed', key,
                     remember=True)
    if stalled:
        return _advance(STALL_STOP, 'the old session stalled: stopped with its successor up')
    rec = newer(newest, c.get('high_c'))
    if rec is not None and rec.get('kind') == 'checkin':
        return _advance(STOPPING, 'check-in record %s' % rec.get('id'))
    if now - c.get('phase_at', now) >= WAIT_CHECKIN_S:
        return _hold('no check-in in %d min; the old session stays idle' % (WAIT_CHECKIN_S // 60),
                     'no-checkin', key)
    return _hold('waiting for its check-in')


def _stop_or_wait(key, c, now, action, note):
    """The stop, or its backoff after a refused one; STOP_TRIES refused, the last backoff out:
    given up."""
    retry = c.get('stop_retry_at')
    if _number(retry) and now < retry:
        return _hold('backoff after a refused stop')
    tries = int(c.get('stop_tries') or 0)
    if tries >= STOP_TRIES:
        return _gone('its stop was refused %d times; the old session stays' % tries,
                     'cross-failed', key, remember=True)
    return Decision(action, note)


def _stopping(args, now):
    key, _entry, c, _live, old, up, _moved = args
    if not old:
        return _gone('its old session has gone: done', 'crossed', c.get('new_name'))
    if not up:
        return _gone('its successor has gone; the old session stays', 'cross-failed', key,
                     remember=True)
    return _stop_or_wait(key, c, now, STOP_OLD, 'its successor checked in')


def _stall_stop(args, stalled, now):
    key, _entry, c, _live, old, up, _moved = args
    if old and stalled:
        return _stop_or_wait(key, c, now, STOP_STALLED, 'stalled')
    if up:
        return _advance(STARTED, 'its successor is up')
    if old:
        return _gone('it is no longer stalled: not stopped', detail=key)
    return _advance(READY, 'the stalled session has gone: start on its own note', stalled=True)


def claims(settings, now):
    """The keys of the crossings whose last phase change is under CLAIM_TTL_S old. LaunchError for
    a bad file: read as empty it would drop the hold."""
    data = state.read_json(state.path(settings, STATE_FILE)) or {}
    crossings = data.get('crossings')
    out = set()
    for key, c in (crossings if isinstance(crossings, dict) else {}).items():
        if isinstance(c, dict) and _number(c.get('phase_at')) and now - c['phase_at'] < CLAIM_TTL_S:
            out.add(key)
    return out


CHECKPOINT = ("The hub's usage crossover: this session's account has reached the weekly limit, and "
              'the hub will move you to account %s. Checkpoint and stand down now with skill '
              'session-handoff: write your state note, post it with hub_handoff_ready (kind '
              'handoff, from %s), reply ready as the skill says and go idle. The hub runs the '
              'next step; do not wait for the PM.')


def checkpoint_text(key, target):
    return CHECKPOINT % (target, key)


def launch_argv(kit, verb, *args):
    return [sys.executable, os.path.join(kit, 'tools', 'launch.py'), verb] + [str(a) for a in args]


def handoff_argv(kit, key, target, state_path=None, what_if=False):
    return (launch_argv(kit, 'handoff', '--session', key, '--account', target)
            + (['--state', state_path] if state_path else [])
            + (['--what-if'] if what_if else []))


def stop_argv(kit, name, pid, successor=None, dry_run=False):
    return (launch_argv(kit, 'stop', '--name', name)
            + (['--successor', successor] if successor else [])
            + ['--pid', str(pid)]
            + (['--dry-run'] if dry_run else []))


def make(sources):
    from kitlib.hubsite import push
    from kitlib.launcher import settings as lsettings
    home = getattr(sources, 'home', None)
    try:
        lsettings.load(home).state_dir()
    except LaunchError:
        return None                     # no launcher state on this machine: nothing to move
    try:
        host = session_watch.host_module()
    except (ImportError, AttributeError, OSError, ValueError):     # registry.RegistryError
        return None

    def deliver(name, text):            # sources.push and .handoffs are read at each run
        if sources.push is None:
            return False
        return sources.push.deliver(name, text).state in (push.WRITTEN, push.UNCERTAIN)

    def handoffs():
        read = getattr(sources, 'handoffs', None)
        return None if read is None else read()

    job = CrossoverJob(lambda: lsettings.load(home), deliver, handoffs, host)
    return jobs.Job(NAME, EVERY_S, job, first_s=FIRST_S)


class CrossoverJob(object):
    """run(ctx) is the job. `load()` the launcher Settings, read at each run so `usage_crossover`
    takes effect with no hub restart; `deliver(name, text)` True when the message was written or
    may have been; `handoffs()` {key: newest hand-off record}, None when the hub has no read yet;
    `host` the watcher readers (session_watch.host_module). What a test also replaces: `procs()`
    the process rows, `child(argv, cwd)` -> (exit code, lines), `clock` epoch seconds, `kit` the
    kit checkout whose tools/launch.py runs."""

    def __init__(self, load, deliver, handoffs, host, procs=None, child=None, clock=None, kit=None):
        self.load, self.deliver, self.handoffs, self.host = load, deliver, handoffs, host
        self.procs = procs or pmstart.list_processes
        self.child = child or C.run_child
        self.clock = clock or time.time
        self.kit = kit or pmstart.KIT_ROOT
        self._noted = {}                # key: the decision note last logged

    def __call__(self, ctx):
        settings = self.load()
        newest = self.handoffs()
        if newest is None:
            return jobs.Skip('the hub gives the job no hand-off read yet')
        now = self.clock()
        doc = self._read(settings, now)
        sessions = state.read_sessions(settings)
        coordinators = state.read_coordinators(settings) or {}
        rows = [(lookup.rc_name(p.get('CommandLine')), p) for p in lookup.launched(self.procs())]
        lives = [Live(n, p.get('ProcessId'), p.get('Created')) for n, p in rows if n]
        pm = {n for n, p in rows if n and _pm_row(n, p, coordinators)}
        accounts = self._accounts(settings)
        run = _Run(self, ctx, settings, doc, now)
        run.log_readings(accounts)
        stalls = self._stalls(settings, lives)
        olds = [c.get('old_name') for c in doc['crossings'].values() if isinstance(c, dict)]
        quiet = self._quiet(set(stalls) | {n for n in olds if n}, now)
        running = _running(lives, coordinators)
        entries = dict(state.entries(sessions))

        def step(key, entry, live):
            crossing = doc['crossings'].get(key)
            name = (crossing or {}).get('old_name') or (live[0].name if live else None)
            return decide(key, entry, crossing, live, stalls.get(name), quiet.get(name), accounts,
                          newest.get(key), doc['starts'], running, doc['landed'],
                          settings.usage_crossover, now,
                          note_at=_mtime((crossing or {}).get('state_path')),
                          cleared=doc['cleared'].get(key), asks=doc['asks'].get(key))

        steps, known = [], set()
        for key in sorted(set(entries) | set(doc['crossings'])):
            entry = entries.get(key)
            stem = (entry or {}).get('stem')
            live = [l for l in lives if C.of_stem(l.name, stem)]
            known.update(l.name for l in live)
            if _pm_entry(key, entry, coordinators) or any(l.name in pm for l in live):
                continue
            d = step(key, entry, live)
            if d.action == ADVANCE:     # a state change is no acting step: decide again from it
                run.advance(key, d)
                d = step(key, entry, live)
            run.settle(key, d)
            steps.append((key, entry, live, d))
        self._others(run, lives, known, pm, stalls, accounts, coordinators)
        run.act(steps, newest, sessions, coordinators)
        return run.finish()

    def _others(self, run, lives, known, pm, stalls, accounts, coordinators):
        """The `pm-limit` and `unmanaged` pushes, once per week window."""
        now = run.now
        acct = coordinators.get('pm_account')
        reset = _hot_reset(None if acct in (None, '') else str(acct), None, accounts, now)
        for l in lives:
            if l.name in pm:
                reset = reset or _hot_reset(None, stalls.get(l.name), accounts, now)
            elif l.name not in known:
                hot = _hot_reset(C.name_account(l.name), stalls.get(l.name), accounts, now)
                if hot is not None:
                    run.once_a_window(l.name, hot, 'unmanaged', l.name)
        if reset is not None:
            run.once_a_window('pm', reset, 'pm-limit', 'pm')

    def _read(self, settings, now):
        """The state file, each part an object; missing is empty, a bad file a failure. A cleared
        mark whose week has reset is dropped."""
        data = state.read_json(state.path(settings, STATE_FILE)) or {}
        for part in ('crossings', 'starts', 'landed', 'told', 'windows', 'readings', 'cleared',
                     'asks'):
            if not isinstance(data.get(part), dict):
                data[part] = {}
        data['cleared'] = {k: v for k, v in data['cleared'].items()
                           if isinstance(v, dict) and _number(v.get('reset')) and v['reset'] > now}
        return data

    def _accounts(self, settings):
        out = {}
        for n in settings.account_numbers():
            _, data = A.newest_usage(settings, n, by='updated')
            out[str(n)] = C.AccountState(A.account_signed_in(settings, n), data)
        return out

    def _stalls(self, settings, lives):
        """{name: (account or None, reset epoch)} of the live sessions whose transcript ends on a
        weekly-limit error. The record must be the live process's: the registry keeps dead ones."""
        pids = {l.pid: l.name for l in lives}
        folders = pmwatch.account_folders(settings)
        out = {}
        for r in self.host.rc_records(pipes.registry_dirs(settings)):
            read = r.get('weekly_stop')
            if read is None or pids.get(r.get('pid')) != r.get('name'):
                continue
            stop = read(list(folders))
            if stop is not None:
                out[r['name']] = (folders.get(stop[0]), stop[1])
        return out

    def _quiet(self, names, now):
        """{name: seconds since its transcript was written} of these sessions (the stalled ones
        and those of open crossings). An unreadable one is left out: unknown is not quiet."""
        finder = self.host.newest_finder()
        out = {}
        for name in sorted(names):
            try:
                t = finder(name)
            except OSError:
                continue
            if t is not None:
                out[name] = now - t
        return out


def _mtime(path):
    """The mtime of a state note, or None: no path, or no file."""
    if not isinstance(path, str) or not path:
        return None
    try:
        return os.path.getmtime(path)
    except (OSError, ValueError):
        return None


def _pm_name(name, coordinators):
    return lstop._pm_named(name) or name == coordinators.get('pm_name')


def _pm_entry(key, entry, coordinators):
    """True for a sessions.json entry that names the PM session."""
    entry = entry or {}
    return any(n and _pm_name(n, coordinators) for n in (key, entry.get('name'), entry.get('stem')))


def _pm_row(name, row, coordinators):
    """True for the PM's process, as launcher.stop refuses it: by name, or by the session id on
    its command line (pm_session_id: a `<` value is the example file's placeholder)."""
    pm_id = coordinators.get('pm_session_id')
    return _pm_name(name, coordinators) or (isinstance(pm_id, str) and not pm_id.startswith('<')
                                            and pm_id in (row.get('CommandLine') or ''))


def _running(lives, coordinators):
    """{account: live sessions on it}: by each session's name, and the PM's account."""
    out = collections.Counter(a for a in (C.name_account(l.name) for l in lives) if a is not None)
    pm = coordinators.get('pm_account')
    if pm not in (None, ''):
        out[str(pm)] += 1
    return out


def _hot_reset(account, stall, accounts, now):
    """The week's reset when this session or account is at the trigger (a stall read ahead, or a
    fresh reading at TRIGGER_WEEK%), else None."""
    if stall is not None and stall[1] > now:
        return stall[1]
    st = accounts.get(account)
    data = st.usage if st is not None else None
    week, reset = window(data, 'seven_day', now)
    if week is not None and week >= TRIGGER_WEEK and is_fresh(data, now):
        return reset
    return None


class _Run(object):
    """One run's work on the state document: every write goes through save(), before the action
    it covers."""

    def __init__(self, job, ctx, settings, doc, now):
        self.job, self.ctx, self.settings, self.doc, self.now = job, ctx, settings, doc, now
        self.notes = []
        self.acted = False
        self.dirty = False

    def save(self):
        state.write_json(state.path(self.settings, STATE_FILE), self.doc)
        self.dirty = False

    def log(self, line):
        state.log(self.settings, LOG, line, datetime.datetime.fromtimestamp(self.now))

    def log_readings(self, accounts):
        for acct, st in sorted(accounts.items(), key=lambda t: int(t[0])):
            five, _ = window(st.usage, 'five_hour', self.now)
            week, reset = window(st.usage, 'seven_day', self.now)
            if week is None:
                continue
            text = 'five=%s week=%s' % ('?' if five is None else int(five), int(week))
            if self.doc['readings'].get(acct) != text:
                self.doc['readings'][acct] = text
                self.dirty = True
                self.log('reading account %s %s updated=%s' % (acct, text,
                                                               (st.usage or {}).get('updated_at')))

    def tell(self, key, d):
        """Push d.word once per change of its mark: told[key] keeps the last one."""
        if not d.word:
            if self.doc['told'].pop(key, None) is not None:
                self.dirty = True
            return
        if self.doc['told'].get(key) == d.mark:
            return
        self.doc['told'][key] = d.mark
        self.dirty = True
        self.ctx.event(d.word, d.detail)

    def once_a_window(self, name, reset, word, detail):
        self.doc['windows'] = {n: t for n, t in self.doc['windows'].items()
                               if _number(t) and t > self.now}
        told = self.doc['windows'].get(name)
        if told is None or abs(told - reset) >= RESET_NEAR_S:   # the sources differ by seconds
            self.doc['windows'][name] = reset
            self.dirty = True
            self.ctx.event(word, detail)

    def advance(self, key, d):
        c = self.doc['crossings'][key]
        c.update(d.fields, phase=d.phase, phase_at=self.now)
        self.dirty = True
        self.log('%s: %s: now %s' % (key, d.note, d.phase))

    def settle(self, key, d):
        """One key's note, and a hold's or a clear's push; a clear drops the crossing."""
        self.notes.append('%s: %s' % (key, d.note))
        if self.job._noted.get(key) != d.note:
            self.job._noted[key] = d.note
            self.log('%s: %s' % (key, d.note))
        if d.action == CLEAR:
            self.doc['crossings'].pop(key, None)
            if d.fields.get('reset') is not None:
                self.doc['cleared'][key] = d.fields
            self.dirty = True
            self.tell(key, d)
        elif d.action == HOLD:
            self.tell(key, d)

    def act(self, steps, newest, sessions, coordinators):
        """The first step in ACTS order: this run's one acting step. A would-cross already told
        is no step: it would keep the others out."""
        chosen = [s for s in steps if s[3].action in ACTS
                  and not (s[3].action == WOULD and self.doc['told'].get(s[0]) == s[3].mark)]
        if not chosen:
            return
        key, entry, live, d = min(chosen, key=lambda s: ACTS.index(s[3].action))
        if self.ctx.stop.is_set():
            self.notes.append('%s: left for the next run (the hub is closing)' % key)
            return
        self.acted = True
        step = getattr(self, '_' + d.action.replace('-', '_'))
        step(key, entry, live, d, newest.get(key), sessions, coordinators)

    def _new_crossing(self, key, entry, live, target, high, phase):
        old = live[0]
        c = {'phase': phase, 'phase_at': self.now, 'asked_at': self.now, 'target': target,
             'account': C.name_account(old.name) or str(entry.get('account')), 'old_name': old.name,
             'old_pid': old.pid, 'old_created': old.created, 'entry_name': entry.get('name'),
             'high_c': high, 'delivered': False}
        self.doc['crossings'][key] = c
        return c

    def _ask(self, key, entry, live, d, newest, sessions, coordinators):
        c = self.doc['crossings'].get(key)
        if c is None:
            c = self._new_crossing(key, entry, live, d.target, c_number(newest), ASKED)
            kept = {k: recent(v, self.now) for k, v in self.doc['asks'].items()}
            self.doc['asks'] = {k: v for k, v in kept.items() if v}
            self.doc['asks'].setdefault(key, []).append(self.now)
        self.save()
        c['delivered'] = bool(self.job.deliver(c['old_name'], checkpoint_text(key, d.target)))
        self.dirty = True
        if d.word:
            self.tell(key, d)
        self.notes.insert(0, '%s: asked to checkpoint for account %s' % (key, d.target))

    def _start(self, key, entry, live, d, newest, sessions, coordinators):
        c = self.doc['crossings'][key]
        c.update(phase=STARTING, phase_at=self.now, target=d.target, high_c=c_number(newest),
                 new_name=handoff.successor_name(entry, d.target, False))
        kept = {k: recent(v, self.now) for k, v in self.doc['starts'].items()}
        self.doc['starts'] = {k: v for k, v in kept.items() if v}
        self.doc['starts'].setdefault(key, []).append(self.now)
        self.doc['landed'][d.target] = self.now
        self.save()                     # before the child: a crash mid-start still counts
        path = None if c.get('stalled') else c.get('state_path')
        self.notes.insert(0, '%s: handing off to account %s' % (key, d.target))
        self._child(handoff_argv(self.job.kit, key, d.target, path))

    def _finish(self, key, entry, live, d, newest, sessions, coordinators):
        c = self.doc['crossings'][key]
        handoff.record_move(self.settings, sessions, coordinators or None, key, entry,
                            c['target'], c['new_name'])
        c.update(phase=STARTED, phase_at=self.now)
        self.dirty = True
        self.notes.insert(0, '%s: sessions.json moved to %s' % (key, c['new_name']))

    def _count_stop(self, c):
        """A stop is counted, and its backoff set, before the child: a refusal waits it out."""
        tries = int(c.get('stop_tries') or 0) + 1
        c.update(stop_tries=tries, stop_retry_at=self.now + BACKOFF_S[min(tries, len(BACKOFF_S)) - 1])

    def _stop_old(self, key, entry, live, d, newest, sessions, coordinators):
        c = self.doc['crossings'][key]
        self._count_stop(c)
        self.save()
        self.notes.insert(0, '%s: stopping %s' % (key, c['old_name']))
        self._child(stop_argv(self.job.kit, c['old_name'], c['old_pid'], c['new_name']))

    def _stop_stalled(self, key, entry, live, d, newest, sessions, coordinators):
        c = self.doc['crossings'].get(key)
        if c is None:
            c = self._new_crossing(key, entry, live, d.target, c_number(newest), STALL_STOP)
            c['stalled'] = True
        if d.word:
            self.tell(key, d)
        self._count_stop(c)
        self.save()
        up = c.get('new_name') in [l.name for l in live]
        self.notes.insert(0, '%s: stopping stalled %s' % (key, c['old_name']))
        self._child(stop_argv(self.job.kit, c['old_name'], c['old_pid'],
                              c['new_name'] if up else None))

    def _would(self, key, entry, live, d, newest, sessions, coordinators):
        """A dry run on a change: nothing starts or stops, and a refusal is only logged."""
        name = live[0].name
        for argv in (handoff_argv(self.job.kit, key, d.target, what_if=True),
                     stop_argv(self.job.kit, name, live[0].pid, dry_run=True)):
            try:
                code, lines = self.job.child(argv, self.job.kit)
            except (subprocess.TimeoutExpired, OSError) as e:
                code, lines = -1, [type(e).__name__]
            self.log('%s: would run %s: exit %s: %s' % (key, argv[2], code,
                                                        (lines or ['no output'])[-1]))
        self.tell(key, d)

    def _child(self, argv):
        what = ' '.join(argv[2:])
        try:
            code, lines = self.job.child(argv, self.job.kit)
        except subprocess.TimeoutExpired:
            raise StepFailed('%s did not end in %d s' % (what, C.START_TIMEOUT_S))
        except OSError as e:
            raise StepFailed('%s could not start: %s' % (what, e))
        self.log('ran %s: exit %s: %s' % (what, code, (lines or ['no output'])[-1]))
        if code != 0:
            raise StepFailed('%s exited %s: %s' % (what, code, (lines or ['no output'])[-1]))

    def finish(self):
        if self.dirty:
            self.save()
        text = '; '.join(self.notes) or 'no session in the sessions file'
        return text if self.acted else jobs.Skip(text)
