"""Claude accounts: which config folder a launch uses, whether it is signed in, how much of its
usage windows is left, and which account to pick (docs/launcher.md "Accounts").

Every Claude session's status line writes usage-status.json into its account's config folder:
{"updated_at": iso, "five_hour": {"used_percentage": n, "resets_at": iso} | null,
 "seven_day": {...} | null}. A window whose resets_at has passed has reset (0%).
"""
import datetime
import json
import os
import re

from kitlib.launcher.settings import LaunchError

CREDENTIALS = '.credentials.json'
USAGE_FILE = 'usage-status.json'
WINDOWS = ('five_hour', 'seven_day')
HEADROOM_LIMIT = 90      # deep and consultant launches refuse an account at or over this (the PM's headroom)
PICK_LIMIT = 95          # pick-account's default
FRACTION = re.compile(r'\.\d{7,}')   # PowerShell ToString('o') writes 7 fraction digits


def parse_time(text):
    """An ISO time (PowerShell's 7-digit fractions and offsets too) as an aware datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    text = FRACTION.sub(lambda m: m.group(0)[:7], text.replace('Z', '+00:00'))   # Python before 3.11 takes 6 digits
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def whole(pct):
    """A percentage as a whole number, rounded half to even as PowerShell's [int] cast did (94.6 is
    95, so a 95% limit counts it as over); None or '' is 0."""
    return int(round(float(pct or 0)))


def short(dt):
    """'ddd HH:mm' in local time, as the PowerShell launchers printed it."""
    return dt.astimezone().strftime('%a %H:%M')


def signed_in(folder):
    return os.path.isfile(os.path.join(folder, CREDENTIALS))


def org_signed_in(folder):
    """An org sign-in keeps .credentials.json empty; its .claude.json names the account instead."""
    try:
        with open(os.path.join(folder, '.claude.json'), encoding='utf-8-sig') as f:
            return bool((json.load(f) or {}).get('oauthAccount'))
    except (OSError, ValueError, AttributeError):
        return False


def account_signed_in(settings, n):
    return any(signed_in(d) for d in settings.account_dirs(n))


def launch_dir(settings, n):
    """The config folder an account-n session starts with: the first of its folders that holds a
    sign-in. LaunchError when none does."""
    dirs = settings.account_dirs(n)
    for d in dirs:
        if signed_in(d):
            return d
    raise LaunchError('Account %s is not signed in: no %s under %s.' % (n, CREDENTIALS, ' or '.join(dirs)))


def org_launch_dir(settings, n):
    d = settings.org_dir(n)
    if not org_signed_in(d):
        raise LaunchError('Org %s is not signed in: sign in under %s (its .claude.json names no '
                          'oauthAccount).' % (n, d))
    return d


def _read(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def newest_usage(settings, n, by='mtime'):
    """(path, data) of the account's freshest usage-status.json, or (None, None). by='mtime' picks
    the newest file write (deep, consultant, pick); by='updated' the newest updated_at (usage)."""
    found = []
    for d in settings.account_dirs(n):
        p = os.path.join(d, USAGE_FILE)
        if not os.path.isfile(p):
            continue
        data = _read(p)
        if data is None:
            continue
        if by == 'updated':
            key = parse_time(data.get('updated_at'))
            key = key.timestamp() if key else float('-inf')
        else:
            key = os.path.getmtime(p)
        found.append((key, p, data))
    if not found:
        return None, None
    _, p, data = max(found, key=lambda t: t[0])
    return p, data


def over_limit(data, now, limit=HEADROOM_LIMIT):
    """The windows at or over `limit`% that have not reset yet, as 'N% of its <window> window
    (resets ddd HH:mm)' texts."""
    out = []
    for k in WINDOWS:
        w = (data or {}).get(k)
        if not isinstance(w, dict) or w.get('used_percentage') is None:
            continue
        reset = parse_time(w.get('resets_at'))
        pct = float(w['used_percentage'])
        if reset and reset > now and pct >= limit:
            out.append('%d%% of its %s window (resets %s)' % (int(pct), k, short(reset)))
    return out


def check_headroom(settings, n, now, force, out, limit=HEADROOM_LIMIT):
    """Refuse (LaunchError) an account at or over `limit`% of a window unless force; say so in out."""
    path, data = newest_usage(settings, n)
    if path is None:
        out.append('Account %s: no %s, so its headroom is not checked.' % (n, USAGE_FILE))
        return
    over = over_limit(data, now, limit)
    if not over:
        return
    msg = 'Account %s is at %s' % (n, ' and '.join(over))
    if not force:
        raise LaunchError("%s; the last %d%% is the PM's headroom. Pick another account, wait for the "
                          'reset, or pass --force.' % (msg, 100 - limit), out)
    out.append('--force: starting on account %s anyway; it is at %s.' % (n, ' and '.join(over)))


def week_rows(settings, now):
    """[(account, pct, reset)] of every signed-in account's seven-day window. A window whose
    reset has passed is fresh (0%) and resets 7 days after the old time; no file reads as 0%,
    resetting a week from now."""
    rows = []
    for n in settings.account_numbers():
        if not account_signed_in(settings, n):
            continue
        pct, reset = 0, now + datetime.timedelta(days=7)
        _, data = newest_usage(settings, n)
        w = (data or {}).get('seven_day')
        if isinstance(w, dict) and parse_time(w.get('resets_at')):
            pct, reset = whole(w.get('used_percentage')), parse_time(w['resets_at'])
            while reset < now:
                reset, pct = reset + datetime.timedelta(days=7), 0
        rows.append((n, pct, reset))
    return rows


def pick(settings, now, limit=PICK_LIMIT):
    """(account, rows): the signed-in account under `limit`% of its week whose week resets
    soonest (use up the allowance that expires first); when every one is over, the soonest reset
    anyway, so something always runs. LaunchError when no account is signed in."""
    rows = week_rows(settings, now)
    if not rows:
        raise LaunchError('No account in launcher.accounts is signed in.')
    ok = [r for r in rows if r[1] < limit]
    chosen = min(ok or rows, key=lambda r: r[2])
    return chosen[0], rows


def explain(rows):
    return ['account %s: %d%% of week, resets %s' % (n, pct, reset.astimezone().strftime('%a %b %d %H:%M'))
            for n, pct, reset in rows]
