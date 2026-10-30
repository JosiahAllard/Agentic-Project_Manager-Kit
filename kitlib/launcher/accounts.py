"""Claude accounts: which config folder a launch uses, whether it is signed in, how much of its
usage windows is left, which account to pick (docs/launcher.md "Accounts"; restart_pick for the
hub's coordinator restart, docs/launcher.md "Hub restart"), and which account
this session runs on (current_account, for the Action Hub's one copy per account).

Every Claude session's status line writes usage-status.json into its account's config folder:
{"updated_at": iso, "five_hour": {"used_percentage": n, "resets_at": iso} | null,
 "seven_day": {...} | null}. A window whose resets_at has passed has reset (0%).
"""
import datetime
import json
import os
import re

from kitlib.config import user as kuser
from kitlib.launcher.settings import LaunchError

CREDENTIALS = '.credentials.json'
USAGE_FILE = 'usage-status.json'
WINDOWS = ('five_hour', 'seven_day')
HEADROOM_LIMIT = 95      # deep and consultant launches refuse an account at or over this (the PM's headroom)
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
    reset has passed is fresh (0%) and resets 7 days after the old time. No file, or a file with
    no seven-day window or no percentage in it, is unknown: pct and reset None, never 0%."""
    rows = []
    for n in settings.account_numbers():
        if not account_signed_in(settings, n):
            continue
        pct, reset = None, None
        _, data = newest_usage(settings, n)
        w = (data or {}).get('seven_day')
        used = w.get('used_percentage') if isinstance(w, dict) else None
        if isinstance(used, (int, float)) and not isinstance(used, bool) and parse_time(w.get('resets_at')):
            pct, reset = whole(used), parse_time(w['resets_at'])
            while reset < now:
                reset, pct = reset + datetime.timedelta(days=7), 0
        rows.append((n, pct, reset))
    return rows


def pick(settings, now, limit=PICK_LIMIT):
    """(account, rows): the signed-in account under `limit`% of its week whose week resets
    soonest (use up the allowance that expires first); when every one is over, the soonest reset
    anyway, so something always runs. An account whose week is unknown (no usage file) counts as
    under the limit, resetting a week from now, as before. LaunchError when no account is signed in."""
    rows = week_rows(settings, now)
    if not rows:
        raise LaunchError('No account in launcher.accounts is signed in.')
    unknown = now + datetime.timedelta(days=7)
    ok = [r for r in rows if r[1] is None or r[1] < limit]
    chosen = min(ok or rows, key=lambda r: unknown if r[2] is None else r[2])
    return chosen[0], rows


def _fresh_week_reset(data, now, stale_minutes, limit):
    """The seven-day reset of a reading that is fresh and under `limit`% in both windows, or None.
    Fresh: an updated_at at most `stale_minutes` whole minutes old (kitlib.pmwatch's stale line).
    A window whose reset has passed counts as 0% and its week moves on 7 days at a time; a window
    with no percentage or no reset time is unknown, and unknown is never under the limit."""
    if not isinstance(data, dict):
        return None
    upd = parse_time(data.get('updated_at'))
    if upd is None or int((now - upd).total_seconds() // 60) > stale_minutes:
        return None
    resets = {}
    for k in WINDOWS:
        w = data.get(k)
        reset = parse_time(w.get('resets_at')) if isinstance(w, dict) else None
        if reset is None:
            return None
        if reset > now:
            pct = w.get('used_percentage')
            if not isinstance(pct, (int, float)) or isinstance(pct, bool) or pct >= limit:
                return None
        resets[k] = reset
    week = resets['seven_day']
    while week <= now:
        week += datetime.timedelta(days=7)
    return week


def restart_pick(states, now, last, stale_minutes, limit=HEADROOM_LIMIT):
    """The account the hub's coordinator restart starts on (docs/launcher.md "Hub restart"): of
    `states` {account: (signed_in, usage data or None)}, the signed-in one with a fresh reading
    under `limit`% of both windows whose week resets soonest; a tie goes to `last`, then the
    lowest number. None when no account qualifies: a missing, stale or unknown reading never does."""
    rows = []
    for n, (signed, data) in states.items():
        reset = _fresh_week_reset(data, now, stale_minutes, limit) if signed else None
        if reset is not None:
            rows.append((reset, str(n) != str(last), int(n) if str(n).isdigit() else float('inf'), str(n)))
    return min(rows)[3] if rows else None


DEFAULT_LABEL = 'default'    # current_account's answer with no launcher accounts or no match
CONFIG_DIR_ENV = 'CLAUDE_CONFIG_DIR'


def _same_folder_key(folder, home):
    """`folder` (absolute, or ~ / ~/<path>) as a comparable key: home expanded, normalised,
    case and slashes folded (os.path.normcase on Windows)."""
    f = folder.strip()
    if f == '~':
        f = home
    elif f.startswith(('~/', '~\\')):
        f = os.path.join(home, f[2:])
    return os.path.normcase(os.path.normpath(f))


def current_account(machine, environ=None, home=None):
    """The label of the Claude account this process runs on: the `launcher.accounts` key (of the
    machine config dict `machine`) whose folder list holds this session's config folder, which is
    CLAUDE_CONFIG_DIR, or Claude Code's default folder ~/.claude when that is unset or empty.
    DEFAULT_LABEL when the machine config has no launcher accounts (a fresh user) or no account
    lists the folder. Pure: it reads only its arguments (environ defaults to os.environ, home to
    the user's home folder) and never raises for a malformed launcher object."""
    launcher = machine.get('launcher') if isinstance(machine, dict) else None
    accts = launcher.get('accounts') if isinstance(launcher, dict) else None
    if not isinstance(accts, dict) or not accts:
        return DEFAULT_LABEL
    env = os.environ if environ is None else environ
    h = home or kuser.home_dir()
    cur = env.get(CONFIG_DIR_ENV) or ''
    here = _same_folder_key(cur if cur.strip() else '~/.claude', h)
    # Numbered labels in number order ("2" before "10"), as the launcher lists them; others after.
    for n in sorted(accts, key=lambda k: (0, int(k), '') if str(k).isdigit() else (1, 0, str(k))):
        dirs = accts[n]
        if not isinstance(dirs, list):
            continue
        if any(isinstance(d, str) and d.strip() and _same_folder_key(d, h) == here for d in dirs):
            return str(n)
    return DEFAULT_LABEL


def resolve_current(settings, now, environ=None):
    """(account, note) for `--account current`: current_account over this machine's launcher
    accounts (`settings.accounts`, `settings.home`). A session whose folder no account lists
    (DEFAULT_LABEL) falls back to pick, and note says so. LaunchError when no account is signed in."""
    label = current_account({'launcher': {'accounts': settings.accounts}}, environ=environ, home=settings.home)
    if label in settings.accounts:
        return label, None
    n, _ = pick(settings, now)
    return n, 'current account not identified; picked %s' % n


def explain(rows):
    return ['account %s: week unknown (no usage reading)' % n if pct is None else
            'account %s: %d%% of week, resets %s' % (n, pct, reset.astimezone().strftime('%a %b %d %H:%M'))
            for n, pct, reset in rows]
