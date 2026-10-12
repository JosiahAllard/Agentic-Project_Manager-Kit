"""Usage watcher for a coordinator. Silent while nothing changes; exits with ONE line when it does.

usage: python ~/.claude/tools/usage/usage_watch.py [--config-dir <dir>] [--poll 300] [--once]

Start it as a BACKGROUND command at coordinator start. It polls the account's
`usage-status.json` and stays quiet, so it costs no turns. It exits - which wakes the
coordinator - only when something the coordinator must act on happens:
  BAND     the five-hour band changed (under-60 / 60-80 / 80-90 / 90-93 / 93+)
  STEP     five_hour rose another 5 points while above 60% (fast burn; replaces re-reading before each dispatch)
  WEEK     seven_day crossed 80% (either direction)
  RESET    a window's resets_at passed (held work may resume)
  STALE    the file is missing or older than 30 min (usage unknown, not 0)
It runs until the process that started it is gone, then stops itself. No time limit.
On waking: act on the line per the repo's coordinator runbook, then start the watcher again.
--once prints the current reading and exits (the replacement for reading the file by hand).
Exit status 0 always; the line is the message.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _usage as U  # noqa: E402

BANDS = ((60, 'under-60: up to four strongest-model agents'),
         (80, '60-80: at most two strongest-model; landings, reviews, cheap work continue'),
         (90, '80-90: nothing new but clerical docs; running agents to a safe stop; tell the owner'),
         (93, '90-93: same, and bring your state note up to date now'),
         (1000, '93+: start nothing at all'))


def band(pct):
    for i, (limit, _) in enumerate(BANDS):
        if pct < limit:
            return i
    return len(BANDS) - 1


def read(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    try:
        updated = dt.datetime.fromisoformat(d['updated_at'][:26] + d['updated_at'][-6:])
    except (KeyError, ValueError):
        return None
    five = d.get('five_hour') or {}
    week = d.get('seven_day') or {}

    def when(w):
        try:
            return dt.datetime.fromisoformat(w['resets_at'][:26] + w['resets_at'][-6:])
        except (KeyError, ValueError, TypeError):
            return None
    return dict(updated=updated, five=int(five.get('used_percentage') or 0), five_reset=when(five),
                week=int(week.get('used_percentage') or 0), week_reset=when(week))


def line(r):
    now = dt.datetime.now(dt.timezone.utc)
    age = int((now - r['updated'].astimezone(dt.timezone.utc)).total_seconds() / 60)
    fr = r['five_reset'].astimezone().strftime('%H:%M') if r['five_reset'] else 'none open'
    wr = r['week_reset'].astimezone().strftime('%m-%d %H:%M') if r['week_reset'] else '?'
    return 'five_hour %d%% (resets %s) | seven_day %d%% (resets %s) | read %d min ago | %s%s' % (
        r['five'], fr, r['week'], wr, age, BANDS[band(r['five'])][1],
        ' | seven_day over 80: ask the owner before any L-sized or strongest-model dispatch' if r['week'] > 80 else '')


def parent_alive(pid):
    if os.name != 'nt':
        return os.getppid() == pid
    import ctypes
    h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(h)
    return bool(ok) and code.value == 259                          # STILL_ACTIVE


def event(last, r, now):
    """The wake-up line for a change from reading `last` to `r` at `now` (both readable, `r`
    fresh), or None when the coordinator need not wake."""
    for key, name in (('five_reset', 'five_hour'), ('week_reset', 'seven_day')):
        if last[key] and last[key].astimezone(dt.timezone.utc) <= now and r[key] != last[key]:
            return 'RESET %s window reset | %s' % (name, line(r))
    if band(r['five']) != band(last['five']):
        return 'BAND five_hour %d%% -> %d%% | %s' % (last['five'], r['five'], line(r))
    if r['five'] >= 60 and r['five'] // 5 > max(last['five'], 60) // 5:
        return 'STEP five_hour %d%% -> %d%% | %s' % (last['five'], r['five'], line(r))
    if (r['week'] > 80) != (last['week'] > 80):
        return 'WEEK seven_day %d%% -> %d%% | %s' % (last['week'], r['week'], line(r))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--config-dir', help='default: CLAUDE_CONFIG_DIR, else ~/.claude')
    ap.add_argument('--poll', type=int, default=300)
    ap.add_argument('--once', action='store_true')
    a = ap.parse_args(argv)
    # A given folder is used as written ('' is the current folder); the home is looked up only
    # when nothing else names one.
    config_dir = a.config_dir
    if config_dir is None:
        config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or U.default_config_dir()
    path = os.path.join(config_dir, 'usage-status.json')
    parent = os.getppid()
    start = time.time()

    first = read(path)
    if a.once:
        print(line(first) if first else 'STALE usage file missing or unreadable: %s (unknown, not 0)' % path)
        return
    if first:
        print('watching %s | %s' % (path, line(first)), flush=True)
    last = first
    while True:
        time.sleep(a.poll)
        if not parent_alive(parent):
            return                                  # PARENT: the coordinator is gone; stop quietly
        r = read(path)
        now = dt.datetime.now(dt.timezone.utc)
        if r is None or (now - r['updated'].astimezone(dt.timezone.utc)).total_seconds() > 1800:
            if last is not None or time.time() - start > 1800:
                print('STALE usage file missing or over 30 min old: unknown, not 0. Tell the owner; no '
                      'strongest-model or L-sized work until it refreshes. ' + (line(r) if r else path))
                return
            continue
        if last is None:
            last = r
            continue
        msg = event(last, r, now)
        if msg:
            print(msg)
            return
        last = r


if __name__ == '__main__':
    main()
