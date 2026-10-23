"""kitlib.maintlint.result: the last run's result.json, in kitlib.dailyfull's pattern: one JSON
file, written whole (a temp file, then os.replace), the last run only (docs/maint-lint.md
"Output").

  default_dir(root, name, home)   `<kit temp>/<name>-maint-lint` (kitlib.config.user.work_temp of
                                  `root`: on the checkout's drive, outside every repository)
  read(path)                      the previous result as a dict, or None (missing or unreadable)
  write(path, result)             the absolute path written
  previous_fingerprints(result)   the fingerprints a result holds (empty for None)
"""
import json
import os

from kitlib.config import ConfigError
from kitlib.config import user as kuser

NAME = 'result.json'
PAGE = 'page.html'


def default_dir(root, name, home=None):
    try:
        base = kuser.work_temp(root, home=home)
    except ConfigError:
        base = kuser.system_temp()
    return os.path.join(base, '%s-maint-lint' % name)


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
