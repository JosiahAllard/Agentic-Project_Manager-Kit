"""The guard's refusal log (docs/guard-v2.md "Refusal log"): one JSON line per deny a hook answers,
appended to `<pm_data>/audit/refusals.jsonl` (the machine config's pm_data.path; kitlib.pmdata)
after the answer is written, so the owner can count which rules refuse which roles without asking
any agent. The folder is the backlog audit log's (kitlib.backlog.audit): made on the first write,
left untracked, and refused to every session's own edit (docs/guard.md rule `pm-data-audit`).

A line holds FIELDS and nothing else: the UTC time, the host, the rule, the role tag the deny's
reason carries ('main' or 'subagent'), the subagent's role name and identity, the session's name
(the launcher's PM_KIT_SESSION_NAME), the tool and the Action's kind, the repository the call ran
in (the name of its main checkout's folder, repo_of) and whether the deny gave an Instead.
Never the reason, the command, the path, the working directory or the Instead text: those carry
what a call touched, and a count needs none of it. MISSING stands for an absent value; a deny
with no rule is UNKNOWN_RULE.

Appends hold the OS lock (kitlib.fs.treelock) on LOCK_FILE beside the log, so lines from
concurrent hooks never interleave. Nothing on the way starts a process: the machine config and
the checkout are read from the filesystem, so a slow or hung git never holds a hook's answer up.
A busy lock is tried every PAUSE seconds for WAIT seconds; one try can add up to treelock's
READ_TRIES * READ_SLEEP (0.5 s) while it reads the holder's record, so the lock wait ends within
BOUND seconds. The log is best effort: record() never raises and never changes a verdict. No
pm_data, a machine config that cannot be read, a fenced test process's real home
(kitlib.config.user.HomeFenced), a busy lock or a failed write: no line.

  entry(decision, action, host, role, environ, now)
                                     the line's fields, in FIELDS order
  repo_of(action, environ)           the repository name for the line, MISSING when unknown or
                                     when it could name the account (see its docstring)
  log_path(pm_data)                  <pm_data>/audit/refusals.jsonl
  append(pm_data, fields, wait)      make the folder when missing and append the line under the
                                     lock; Busy when the lock stayed held for `wait` seconds,
                                     OSError or kitlib.fs.treelock.TreeLockError when it cannot
  record(decision, action, host, role, environ, home, now)
                                     the whole step for a deny: the log's path when a line was
                                     appended, else None; never raises
"""
import collections
import datetime
import json
import os
import time

from kitlib import sessionmark

FOLDER = 'audit'
FILE = 'refusals.jsonl'
FIELDS = ('utc', 'host', 'rule', 'role', 'agent_role', 'agent', 'session', 'tool', 'kind', 'repo',
          'instead_given')
MISSING = '-'
UNKNOWN_RULE = 'unknown'
LOCK_FILE = '.refusals.lock'   # beside the log, in the audit folder nothing commits
WAIT = 1.0
PAUSE = 0.05
BOUND = WAIT + 0.5 + PAUSE      # the lock wait's worst case, in seconds (module docstring)
GIT_DIR = '.git'


class Busy(Exception):
    """The log's lock stayed held for the whole wait."""


def _text(value):
    value = ' '.join(str(value).split()) if value is not None else ''
    return value or MISSING


def _none(path):
    return None


def repo_of(action, environ=None):
    """The folder name of the main checkout `action` ran in (its workdir, else its cwd, else this
    process's folder), read from the filesystem only (kitlib.guard.place, no git child). Only a
    common dir that is `<checkout>/.git` names one: the checkout itself for a main checkout, the
    main checkout for a linked worktree. MISSING when the filesystem cannot tell, for any other
    layout (a submodule, a separate git dir), and for a name that could be the account's: the
    user-profile folder (USERPROFILE) or a folder named as the account (USERNAME)."""
    from kitlib.git import repo
    from kitlib.guard import paths, place
    env = os.environ if environ is None else environ
    where = (getattr(action, 'workdir', None) or getattr(action, 'cwd', None)) if action is not None else None
    found = place.classify(where or os.getcwd(), fallback=_none)
    common = getattr(found, 'common', None) if found is not None else None
    if not common or getattr(found, 'kind', None) not in (repo.MAIN, repo.LINKED):
        return MISSING
    common = common.rstrip('/\\')
    checkout = os.path.dirname(common)
    name = os.path.basename(checkout)
    if not name or os.path.basename(common).lower() != GIT_DIR:
        return MISSING
    if found.kind == repo.MAIN and paths.norm(checkout) != paths.norm(found.top or ''):
        return MISSING
    profile = env.get('USERPROFILE')
    if profile and paths.norm(checkout) == paths.norm(profile.rstrip('/\\')):
        return MISSING
    if name.lower() == (env.get('USERNAME') or '').strip().lower():
        return MISSING
    return name


def entry(decision, action, host, role, environ=None, now=None):
    env = os.environ if environ is None else environ
    now = now or datetime.datetime.now(datetime.timezone.utc)
    utc = now.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    agent = getattr(action, 'agent', None) if action is not None else None
    values = (
        utc,
        _text(host),
        _text(getattr(decision, 'rule', None) or UNKNOWN_RULE),
        _text(role),
        _text(getattr(action, 'agent_role', None) if agent else None),
        _text(agent),
        _text(env.get(sessionmark.SESSION_NAME_ENV)),
        _text(getattr(action, 'tool', None) if action is not None else None),
        _text(getattr(action, 'kind', None) if action is not None else None),
        repo_of(action, env),
    )
    fields = collections.OrderedDict(zip(FIELDS, values))
    fields['instead_given'] = bool(getattr(decision, 'instead', None))
    return fields


def log_path(pm_data):
    return os.path.join(pm_data, FOLDER, FILE)


def append(pm_data, fields, wait=WAIT):
    from kitlib.fs import treelock
    path = log_path(pm_data)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    line = json.dumps(fields) + '\n'
    lock = treelock.TreeLock(os.path.join(folder, LOCK_FILE), what='append a refusal line')
    deadline = time.monotonic() + wait
    while True:
        try:
            lock.acquire()
            break
        except treelock.TreeBusy:
            if time.monotonic() >= deadline:
                raise Busy('%s stayed held for %s s' % (lock.path, wait))
        time.sleep(PAUSE)
    try:
        with open(path, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(line)
    finally:
        lock.release()
    return path


def record(decision, action, host, role, environ=None, home=None, now=None):
    try:
        if getattr(decision, 'verdict', None) != 'deny':
            return None
        from kitlib import pmdata
        settings = pmdata.machine_settings(home)
        if not settings or not settings.get('path'):
            return None
        return append(settings['path'], entry(decision, action, host, role, environ, now), WAIT)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:                       # HomeFenced too: a log line is never worth a verdict
        return None
