"""Every account's plan usage, as lines (docs/launcher.md "Usage"); kitlib.pmwatch parses them.

  account N: five_hour=X% (resets ddd HH:mm)  seven_day=Y% (resets ...)  [updated M min ago]
  account N: no usage file
  current account: N            (the one whose file was written in the last 15 minutes)
  account codex: five_hour=reset  seven_day=Z% (resets ...)  [updated M min ago]

A file refreshes only while a session on that account runs, so "updated" says how fresh it is; a
window whose resets_at has passed reads `reset`; a window the file does not give (null, no
resets_at, no percentage) reads `?`, which kitlib.pmwatch keeps as unknown, never 0. Each account's newest file by updated_at is read.
Codex's weekly limit is the last rate_limits event of its newest session log; it has no five-hour
window.
"""
import datetime
import os
import re

from kitlib.launcher import accounts as A

FRESH_MINUTES = 15
_CODEX = re.compile(r'"primary":\{"used_percent":([\d.]+),"window_minutes":\d+,"resets_at":(\d+)')


def _window(name, w, now):
    if not isinstance(w, dict):
        return '%s=?' % name
    reset = A.parse_time(w.get('resets_at'))
    if reset is None:
        return '%s=?' % name
    if reset < now:
        return '%s=reset' % name
    pct = w.get('used_percentage')
    if not isinstance(pct, (int, float)) or isinstance(pct, bool):
        return '%s=?' % name
    return '%s=%d%% (resets %s)' % (name, A.whole(pct), A.short(reset))


def account_lines(settings, now):
    out, current, freshest = [], None, FRESH_MINUTES
    for n in settings.account_numbers():
        _, data = A.newest_usage(settings, n, by='updated')
        if data is None:
            out.append('account %s: no usage file' % n)
            continue
        upd = A.parse_time(data.get('updated_at'))
        age = int((now - upd).total_seconds() // 60) if upd else None
        parts = [_window(k, data.get(k), now) for k in A.WINDOWS]
        out.append('account %s: %s  [updated %s min ago]' % (n, '  '.join(parts), '?' if age is None else age))
        if age is not None and age <= freshest:
            current, freshest = n, age
    out.append('current account: %s' % (current or 'unknown (no usage file written in the last %d min)'
                                         % FRESH_MINUTES))
    return out


def newest_codex_log(home):
    root = os.path.join(home, '.codex', 'sessions')
    best = None
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.endswith('.jsonl'):
                p = os.path.join(dp, f)
                try:
                    m = os.path.getmtime(p)
                except OSError:
                    continue
                if best is None or m > best[0]:
                    best = (m, p)
    return best


def codex_line(home, now):
    best = newest_codex_log(home)
    hit = None
    if best:
        try:
            with open(best[1], encoding='utf-8', errors='replace') as f:
                for line in f:
                    for m in _CODEX.finditer(line):
                        hit = m
        except OSError:
            hit = None
    if not hit:
        return 'account codex: no usage data'
    pct = A.whole(hit.group(1))
    reset = datetime.datetime.fromtimestamp(int(hit.group(2)), datetime.timezone.utc)
    age = int((now.timestamp() - best[0]) // 60)
    wk = 'seven_day=reset' if reset < now else 'seven_day=%d%% (resets %s)' % (pct, A.short(reset))
    return 'account codex: five_hour=reset  %s  [updated %d min ago]' % (wk, age)


def report(settings, now=None):
    """All the lines, accounts first, then Codex."""
    now = now or datetime.datetime.now().astimezone()
    return account_lines(settings, now) + [codex_line(settings.home, now)]
