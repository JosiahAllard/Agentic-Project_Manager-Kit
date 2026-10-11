"""The launcher's state files, in launcher.state_dir (docs/launcher.md "State files"):

  sessions.json    {key: {repo, agent, stem, state, brief?, account, name, autostart?}}: every
                   long-running session; handoff and reboot rewrite account and name. A key
                   starting with "_" is a note. agent true runs the coordinator agent, a string
                   that agent, false a plain session.
  coordinators.json {repo: account, pm_session_id, pm_folder, pm_account}: the account each
                   repo's main coordinator is on, and the PM session reboot resumes. Its old
                   name present and the new one absent is a refusal that names the rename, never
                   read in its place.
  *.log            stop.log and after-reboot.log, one line per action.
JSON is read with or without a BOM and written UTF-8 without one.
"""
import datetime
import json
import os

from kitlib.launcher.settings import LaunchError

SESSIONS = 'sessions.json'
COORDINATORS = 'coordinators.json'
OLD_COORDINATORS = 'conductors.json'   # the old name, refused, never read (removed after the switch, W2b)


def path(settings, name):
    return os.path.join(settings.state_dir(), name)


def read_json(p):
    try:
        with open(p, encoding='utf-8-sig') as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise LaunchError('cannot read %s: %s' % (p, e))
    if not isinstance(data, dict):
        raise LaunchError('%s must hold a JSON object' % p)
    return data


def write_json(p, data):
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(data, indent=4, ensure_ascii=False) + '\n')
    os.replace(tmp, p)


def read_sessions(settings):
    p = path(settings, SESSIONS)
    data = read_json(p)
    if data is None:
        raise LaunchError('No %s: copy docs/launcher.sessions.example.json to it.' % p)
    return data


class RenameNeeded(LaunchError):
    """A state file still under its old name: the owner renames it; nothing reads the old one."""


def read_coordinators(settings):
    """coordinators.json, or None when it is absent. Refused (RenameNeeded) while only the
    old-named file exists, so a forgotten rename is said, not silently read or ignored."""
    p = path(settings, COORDINATORS)
    data = read_json(p)
    if data is None and os.path.exists(path(settings, OLD_COORDINATORS)):
        raise RenameNeeded('rename %s to %s (the state file was renamed; docs/launcher.md "State files")'
                           % (path(settings, OLD_COORDINATORS), COORDINATORS))
    return data


def entries(sessions):
    """(key, entry) of the real entries: not a "_" note, an object."""
    return [(k, v) for k, v in sessions.items() if not k.startswith('_') and isinstance(v, dict)]


def log(settings, name, line, now=None):
    stamp = (now or datetime.datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
    with open(path(settings, name), 'a', encoding='utf-8') as f:
        f.write('%s %s\n' % (stamp, line))
