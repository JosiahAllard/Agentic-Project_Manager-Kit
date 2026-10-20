"""The backlog's audit log (docs/backlog.md "Audit log"): one JSON line per write whose rows carry
an AUDITED risk (kitlib/backlog/roles.py), appended by tools/backlog.py after the store write
publishes it, to `<pm_data>/audit/backlog.jsonl` (the machine config's pm_data.path;
kitlib.pmdata). The folder is made on the first write and left untracked: nothing commits it and
nothing may count on its being committed. Only the tool writes there; the guard refuses every
other session's write (docs/guard.md rule `pm-data-audit`).

A line holds FIELDS, never the command's text: the verb, the record id, the session's role as the
tool's own check reads it (kitlib.backlog.policy.cli_role), the session's name (`--session`, else
the launcher's PM_KIT_SESSION_NAME), the UTC time, and the store commit the write published.
MISSING stands for a value that is absent, and for a role the map does not list.

  audited(rows)                      true when a row of `rows` (policy.sort's names) is AUDITED
  role_of(environ)                   the session's role for the line
  session_of(given, environ)         the session's name for the line
  entry(verb, rid, role, session, sha, now)
                                     the line's fields, in FIELDS order
  log_path(pm_data)                  <pm_data>/audit/backlog.jsonl
  pm_data_path(home)                 (pm_data.path or None, why it cannot be read or None)
  append(pm_data, fields)            make the folder when it is missing and append the line;
                                     OSError when it cannot
  after_write(verb, rid, rows, sha, session, environ, home, now)
                                     the whole step: None when there is nothing to say, else
                                     (label, text), NOTE or WARNING, for the tool to print
"""
import collections
import datetime
import json
import os

from kitlib import sessionmark

from . import roles

FOLDER = 'audit'
FILE = 'backlog.jsonl'
FIELDS = ('verb', 'id', 'role', 'session', 'utc', 'store_sha')
MISSING = '-'
NOTE, WARNING = 'NOTE', 'WARNING'


def audited(rows):
    return any(roles.ACTIONS[name].risk in roles.AUDITED for name in rows or ())


def role_of(environ=None):
    from . import policy
    return policy.cli_role(environ) or MISSING


def session_of(given, environ=None):
    env = os.environ if environ is None else environ
    return given or (env.get(sessionmark.SESSION_NAME_ENV) or '').strip() or MISSING


def entry(verb, rid, role, session, sha, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    utc = now.astimezone(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    values = (verb, rid, role, session, utc, sha)
    return collections.OrderedDict((k, v or MISSING) for k, v in zip(FIELDS, values))


def log_path(pm_data):
    return os.path.join(pm_data, FOLDER, FILE)


def pm_data_path(home=None):
    from kitlib import pmdata
    from kitlib.config.types import ConfigError
    try:
        settings = pmdata.machine_settings(home)
    except (ConfigError, pmdata.PmDataError, OSError) as exc:
        return None, ' '.join(str(exc).split())
    return (settings['path'] if settings else None), None


def append(pm_data, fields):
    path = log_path(pm_data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8', newline='\n') as fh:
        fh.write(json.dumps(fields) + '\n')
    return path


def after_write(verb, rid, rows, sha, session=None, environ=None, home=None, now=None):
    if not audited(rows):
        return None
    path, why = pm_data_path(home)
    if why:
        return WARNING, ('the write stands, but its audit line was not written: the machine '
                         'config\'s pm_data cannot be read (%s)' % why)
    if not path:
        return NOTE, ('no audit line: the machine config names no pm_data, so there is no '
                      '<pm_data>/%s/%s to append to' % (FOLDER, FILE))
    fields = entry(verb, rid, role_of(environ), session_of(session, environ), sha, now)
    try:
        append(path, fields)
    except OSError as exc:
        return WARNING, ('the write stands, but its audit line was not appended to %s: %s'
                         % (log_path(path), ' '.join(str(exc).split())))
    return None
