"""kitlib.maintlint.result: the last run's result.json, in kitlib.dailyfull's pattern: one JSON
file, written whole (a temp file, then os.replace), the last run only (docs/maint-lint.md
"Output").

  default_dir(name, home)         `(<pm_data>/maint-lint/<name>, None)`: a stable folder beside
                                  the backlog's audit/ folder (the machine config's pm_data.path,
                                  kitlib.backlog.audit.pm_data_path), left untracked like it; or
                                  `(None, why)` when there is no pm_data
  read(path)                      the previous result as a dict, or None (missing or unreadable)
  write(path, result)             the absolute path written
  previous_fingerprints(result)   the fingerprints a result holds (empty for None)
"""
import json
import os

from kitlib.backlog import audit

NAME = 'result.json'
PAGE = 'page.html'
FOLDER = 'maint-lint'


def default_dir(name, home=None):
    pm_data, why = audit.pm_data_path(home)
    if not pm_data:
        return None, why or 'no pm_data.path in the machine config'
    return os.path.join(pm_data, FOLDER, name), None


def read(path):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(path, result):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
        json.dump(result, fh, indent=1, sort_keys=True)
        fh.write('\n')
    os.replace(tmp, path)
    return path


def previous_fingerprints(result):
    if not result:
        return set()
    return set(f.get('fingerprint') for f in result.get('findings') or () if isinstance(f, dict))
