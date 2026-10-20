"""The guard's refusal log (docs/guard-v2.md "Refusal log"): one JSON line per deny a hook answers,
appended to `<pm_data>/audit/refusals.jsonl` (the machine config's pm_data.path; kitlib.pmdata)
after the answer is written, so the owner can count which rules refuse which roles without asking
any agent. The folder is the backlog audit log's (kitlib.backlog.audit): made on the first write,
left untracked, and refused to every session's own edit (docs/guard.md rule `pm-data-audit`).

A line holds FIELDS and nothing else: the UTC time, the host, the rule, the role tag the deny's
reason carries ('main' or 'subagent'), the subagent's role name and identity, the session's name
(the launcher's PM_KIT_SESSION_NAME), the tool and the Action's kind, the repository the call ran
in (the name of the folder that holds its git common dir) and whether the deny gave an Instead.
Never the reason, the command, the path, the working directory or the Instead text: those carry
what a call touched, and a count needs none of it. MISSING stands for an absent value; a deny
with no rule is UNKNOWN_RULE.

Appends are serialised by kitlib.fs.writelock (prefix LOCK_PREFIX, WAIT seconds), so lines from
concurrent hooks never interleave. The log is best effort: record() never raises and never
changes a verdict. No pm_data, a machine config that cannot be read, a fenced test process's
real home (kitlib.config.user.HomeFenced), a busy lock or a failed write: no line.

  entry(decision, action, host, role, environ, now)
                                     the line's fields, in FIELDS order
  repo_of(action)                    the repository name for the line, MISSING when unknown
  log_path(pm_data)                  <pm_data>/audit/refusals.jsonl
  append(pm_data, fields, wait)      make the folder when missing and append the line under the
                                     write lock; OSError or kitlib.fs.writelock.WriteLockError
                                     when it cannot
  record(decision, action, host, role, environ, home, now)
                                     the whole step for a deny: the log's path when a line was
                                     appended, else None; never raises
"""
import collections
import datetime
import json
import os

from kitlib import sessionmark

FOLDER = 'audit'
FILE = 'refusals.jsonl'
FIELDS = ('utc', 'host', 'rule', 'role', 'agent_role', 'agent', 'session', 'tool', 'kind', 'repo',
          'instead_given')
MISSING = '-'
UNKNOWN_RULE = 'unknown'
LOCK_PREFIX = 'kit-refusals'
WAIT = 1.0


def _text(value):
    value = ' '.join(str(value).split()) if value is not None else ''
    return value or MISSING


def _none(path):
    return None


def repo_of(action):
    """The name of the folder holding the git common dir of the checkout `action` ran in (its
    workdir, else its cwd, else this process's folder), read from the filesystem only
    (kitlib.guard.place, no git child); MISSING when the filesystem cannot tell."""
    from kitlib.guard import place
    where = (getattr(action, 'workdir', None) or getattr(action, 'cwd', None)) if action is not None else None
    found = place.classify(where or os.getcwd(), fallback=_none)
    common = getattr(found, 'common', None) if found is not None else None
    if not common:
        return MISSING
    return os.path.basename(os.path.dirname(common.rstrip('/\\'))) or MISSING


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
        repo_of(action),
    )
    fields = collections.OrderedDict(zip(FIELDS, values))
    fields['instead_given'] = bool(getattr(decision, 'instead', None))
    return fields


def log_path(pm_data):
    return os.path.join(pm_data, FOLDER, FILE)


def append(pm_data, fields, wait=WAIT):
    from kitlib.fs import writelock
    path = log_path(pm_data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps(fields) + '\n'
    with writelock.locked(path, LOCK_PREFIX, wait, what='append a refusal line to'):
        with open(path, 'a', encoding='utf-8', newline='\n') as fh:
            fh.write(line)
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
